//! Osprey desktop shell (Tauri 2).
//!
//! The security-critical piece is `oauth_connect`: connectors are authorized by the
//! **user, in their own system browser**, via a loopback redirect handled here — not
//! through any AI/MCP layer. The desktop app relays only the short-lived `code` to
//! the Osprey backend, which performs the confidential token exchange and seals the
//! tokens server-side. Provider tokens never live in the client.

use std::sync::Mutex;

use serde::{Deserialize, Serialize};
use tauri::{
    menu::{Menu, MenuItem},
    tray::TrayIconBuilder,
    Manager, State,
};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

/// Backend session set by the frontend after login.
#[derive(Default)]
struct Session {
    base_url: Mutex<String>,
    token: Mutex<String>,
}

/// URL of the bundled backend, once it has been spawned and is answering.
#[derive(Default)]
struct Sidecar {
    url: Mutex<Option<String>>,
}

/// Ask the OS for a free loopback port by binding port 0 and releasing it.
fn free_port() -> Result<u16, String> {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").map_err(|e| e.to_string())?;
    listener
        .local_addr()
        .map(|a| a.port())
        .map_err(|e| e.to_string())
}

/// Where the frontend should talk to.
///
/// Returns the bundled backend's URL, or `null` if it is not running — in which case
/// the UI falls back to asking the user for one, so a desktop build can still point
/// at a shared server.
#[tauri::command]
fn backend_url(sidecar: State<Sidecar>) -> Option<String> {
    sidecar.url.lock().unwrap().clone()
}

/// Start the bundled backend on a free loopback port and wait until it answers.
///
/// Tauri kills sidecars when the app exits, so there is no separate teardown: the
/// backend's lifetime is the window's. Failure is non-fatal — the app still runs as a
/// viewer against a backend the user supplies.
fn spawn_backend(app: &tauri::AppHandle) -> Result<String, String> {
    use tauri_plugin_shell::ShellExt;

    let port = free_port()?;
    let url = format!("http://127.0.0.1:{port}");

    let (mut rx, _child) = app
        .shell()
        .sidecar("osprey-backend")
        .map_err(|e| format!("sidecar not bundled: {e}"))?
        .args(["--port", &port.to_string()])
        .spawn()
        .map_err(|e| format!("could not start the backend: {e}"))?;

    // Drain the child's output so its pipe never fills and blocks it.
    tauri::async_runtime::spawn(async move {
        use tauri_plugin_shell::process::CommandEvent;
        while let Some(event) = rx.recv().await {
            if let CommandEvent::Stderr(line) | CommandEvent::Stdout(line) = event {
                eprintln!("[backend] {}", String::from_utf8_lossy(&line).trim_end());
            }
        }
    });

    Ok(url)
}

/// Poll /health until the backend is ready. Called off the UI thread.
async fn wait_until_healthy(url: &str) -> bool {
    let client = reqwest::Client::new();
    for _ in 0..60 {
        if let Ok(resp) = client
            .get(format!("{url}/health"))
            .timeout(std::time::Duration::from_secs(2))
            .send()
            .await
        {
            if resp.status().is_success() {
                return true;
            }
        }
        tokio::time::sleep(std::time::Duration::from_millis(500)).await;
    }
    false
}

#[derive(Deserialize)]
struct AuthorizeResp {
    authorize_url: String,
    state: String,
}

#[derive(Serialize)]
struct ExchangeReq {
    code: String,
    state: String,
    redirect_uri: String,
}

#[tauri::command]
fn set_session(session: State<Session>, base_url: String, token: String) {
    *session.base_url.lock().unwrap() = base_url;
    *session.token.lock().unwrap() = token;
}

/// Full desktop-driven OAuth flow. Returns the created connection as JSON.
#[tauri::command]
async fn oauth_connect(
    session: State<'_, Session>,
    source_type: String,
    project_id: String,
) -> Result<serde_json::Value, String> {
    let base_url = session.base_url.lock().unwrap().clone();
    let token = session.token.lock().unwrap().clone();
    if base_url.is_empty() || token.is_empty() {
        return Err("not signed in".into());
    }

    // 1) Bind a loopback listener; its port defines the redirect URI.
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .map_err(|e| format!("bind failed: {e}"))?;
    let port = listener.local_addr().map_err(|e| e.to_string())?.port();
    let redirect_uri = format!("http://127.0.0.1:{port}/callback");

    // 2) Ask the backend for the provider consent URL (+ signed state / PKCE).
    let http = reqwest::Client::new();
    let authorize: AuthorizeResp = http
        .post(format!("{base_url}/connections/authorize"))
        .bearer_auth(&token)
        .json(&serde_json::json!({
            "project_id": project_id,
            "source_type": source_type,
            "redirect_uri": redirect_uri,
        }))
        .send()
        .await
        .map_err(|e| format!("authorize request failed: {e}"))?
        .error_for_status()
        .map_err(|e| format!("authorize rejected: {e}"))?
        .json()
        .await
        .map_err(|e| e.to_string())?;

    // 3) Open the consent page in the user's system browser.
    open_url(&authorize.authorize_url);

    // 4) Wait for the provider to redirect back with ?code=...&state=...
    let (code, state) = wait_for_code(listener).await?;
    if state != authorize.state {
        return Err("state mismatch — possible CSRF; aborting".into());
    }

    // 5) Relay the code; the backend exchanges + seals the tokens.
    let connection: serde_json::Value = http
        .post(format!("{base_url}/connections/exchange"))
        .bearer_auth(&token)
        .json(&ExchangeReq {
            code,
            state,
            redirect_uri,
        })
        .send()
        .await
        .map_err(|e| format!("exchange request failed: {e}"))?
        .error_for_status()
        .map_err(|e| format!("exchange rejected: {e}"))?
        .json()
        .await
        .map_err(|e| e.to_string())?;

    Ok(connection)
}

/// Accept one loopback request and pull `code` + `state` from the query string.
async fn wait_for_code(listener: TcpListener) -> Result<(String, String), String> {
    let (mut stream, _) = listener.accept().await.map_err(|e| e.to_string())?;
    let mut buf = vec![0u8; 4096];
    let n = stream.read(&mut buf).await.map_err(|e| e.to_string())?;
    let request = String::from_utf8_lossy(&buf[..n]);
    let first_line = request.lines().next().unwrap_or("");
    // "GET /callback?code=...&state=... HTTP/1.1"
    let target = first_line.split_whitespace().nth(1).unwrap_or("");
    let query = target.split_once('?').map(|(_, q)| q).unwrap_or("");

    let mut code = None;
    let mut state = None;
    for pair in query.split('&') {
        if let Some((k, v)) = pair.split_once('=') {
            let val = urlencoding::decode(v)
                .map(|c| c.into_owned())
                .unwrap_or_default();
            match k {
                "code" => code = Some(val),
                "state" => state = Some(val),
                _ => {}
            }
        }
    }

    let body = "<html><body style='font-family:sans-serif;background:#0E1A2B;color:#F6F7F9;\
text-align:center;padding-top:20vh'><h2>Osprey connected \u{2713}</h2>\
<p>You can close this tab and return to the app.</p></body></html>";
    let response = format!(
        "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
        body.len(),
        body
    );
    let _ = stream.write_all(response.as_bytes()).await;

    match (code, state) {
        (Some(c), Some(s)) => Ok((c, s)),
        _ => Err("no authorization code in callback".into()),
    }
}

fn open_url(url: &str) {
    #[cfg(target_os = "windows")]
    let _ = std::process::Command::new("cmd")
        .args(["/C", "start", "", url])
        .spawn();
    #[cfg(target_os = "macos")]
    let _ = std::process::Command::new("open").arg(url).spawn();
    #[cfg(all(unix, not(target_os = "macos")))]
    let _ = std::process::Command::new("xdg-open").arg(url).spawn();
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(Session::default())
        .manage(Sidecar::default())
        .setup(|app| {
            // Start the bundled backend, then record its URL once it answers so the
            // frontend can pick it up. Non-fatal: without it the app is a viewer.
            match spawn_backend(app.handle()) {
                Ok(url) => {
                    let handle = app.handle().clone();
                    tauri::async_runtime::spawn(async move {
                        if wait_until_healthy(&url).await {
                            *handle.state::<Sidecar>().url.lock().unwrap() = Some(url.clone());
                            eprintln!("backend ready on {url}");
                        } else {
                            eprintln!("bundled backend never became healthy");
                        }
                    });
                }
                Err(err) => eprintln!("no bundled backend ({err}); expecting an external one"),
            }

            // System-tray presence — the "always-on" desktop surface.
            let show = MenuItem::with_id(app, "show", "Open Osprey", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show, &quit])?;
            TrayIconBuilder::with_id("osprey-tray")
                .menu(&menu)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "show" => {
                        if let Some(w) = app.get_webview_window("main") {
                            let _ = w.show();
                            let _ = w.set_focus();
                        }
                    }
                    "quit" => app.exit(0),
                    _ => {}
                })
                .build(app)?;
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            set_session,
            oauth_connect,
            backend_url
        ])
        .run(tauri::generate_context!())
        .expect("error while running Osprey");
}
