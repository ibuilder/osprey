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
        .setup(|app| {
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
        .invoke_handler(tauri::generate_handler![set_session, oauth_connect])
        .run(tauri::generate_context!())
        .expect("error while running Osprey");
}
