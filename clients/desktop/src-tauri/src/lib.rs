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
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Manager, State,
};
use tauri_plugin_autostart::{MacosLauncher, ManagerExt};
use tauri_plugin_notification::NotificationExt;

/// Passed by the login item: start in the tray rather than opening a window.
const BACKGROUND_ARG: &str = "--background";
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

/// Backend session set by the frontend after login.
#[derive(Default)]
struct Session {
    base_url: Mutex<String>,
    token: Mutex<String>,
}

/// The bundled backend: its URL once healthy, and its process handle.
///
/// The handle is retained deliberately. Dropping `CommandChild` does not terminate
/// the process, so letting it fall out of scope would leak an `osprey-backend`
/// holding the port and the SQLite file after a crash or force-quit.
#[derive(Default)]
struct Sidecar {
    url: Mutex<Option<String>>,
    /// "starting" | "ready" | "unavailable". Reported to the UI so it can show
    /// progress while the backend boots, and stop waiting the moment we know there
    /// is no sidecar to wait for.
    status: Mutex<Option<String>>,
    child: Mutex<Option<tauri_plugin_shell::process::CommandChild>>,
}

impl Sidecar {
    /// Stop the backend. Safe to call more than once.
    fn shutdown(&self) {
        if let Some(child) = self.child.lock().unwrap().take() {
            if let Err(err) = child.kill() {
                eprintln!("could not stop the backend: {err}");
            }
        }
    }
}

/// Ask the OS for a free loopback port by binding port 0 and releasing it.
fn free_port() -> Result<u16, String> {
    let listener = std::net::TcpListener::bind("127.0.0.1:0").map_err(|e| e.to_string())?;
    listener
        .local_addr()
        .map(|a| a.port())
        .map_err(|e| e.to_string())
}

/// Where the frontend should talk to, and whether it is worth waiting.
///
/// Reporting "unavailable" explicitly matters: without it the UI cannot tell a slow
/// cold start from a build that has no sidecar, and would sit on a progress message
/// for its whole timeout before letting the user enter a URL.
#[tauri::command]
fn backend_status(sidecar: State<Sidecar>) -> serde_json::Value {
    let status = sidecar
        .status
        .lock()
        .unwrap()
        .clone()
        .unwrap_or_else(|| "starting".to_string());
    serde_json::json!({ "status": status, "url": sidecar.url.lock().unwrap().clone() })
}

/// Where the frozen backend lives inside the bundle.
///
/// It ships as a PyInstaller *directory* build rather than a single self-extracting
/// executable: onefile unpacks the interpreter to a temp dir on every launch, which
/// antivirus engines flag and which costs a second or two of cold start. So the
/// backend is a bundled resource, not a Tauri `externalBin`.
#[cfg(target_os = "windows")]
const BACKEND_EXE: &str = "backend/osprey-backend.exe";
#[cfg(not(target_os = "windows"))]
const BACKEND_EXE: &str = "backend/osprey-backend";

/// Start the bundled backend on a free loopback port.
///
/// The child handle is stored in `Sidecar` so it can be killed on exit — dropping it
/// would not stop the process. Failure is non-fatal: the app still runs as a viewer
/// against a backend the user supplies.
fn spawn_backend(app: &tauri::AppHandle) -> Result<String, String> {
    use tauri_plugin_shell::ShellExt;

    let exe = app
        .path()
        .resolve(BACKEND_EXE, tauri::path::BaseDirectory::Resource)
        .map_err(|e| format!("backend not bundled: {e}"))?;
    if !exe.exists() {
        return Err(format!(
            "backend missing from the bundle at {}",
            exe.display()
        ));
    }
    // Shell::command takes the program as a string, not a path.
    let program = exe.to_string_lossy().to_string();

    // free_port() is inherently racy: the probe listener is released before the
    // backend binds, so another process can take the port in between. Rather than
    // fail cryptically, try a few times.
    let mut last_err = String::new();
    for attempt in 1..=3 {
        let port = free_port()?;
        let url = format!("http://127.0.0.1:{port}");

        let spawned = app
            .shell()
            .command(&program)
            .args(["--port", &port.to_string()])
            .spawn();

        match spawned {
            Ok((rx, child)) => {
                // Keep the handle so the process can be terminated later.
                *app.state::<Sidecar>().child.lock().unwrap() = Some(child);
                drain_output(rx);
                return Ok(url);
            }
            Err(err) => {
                last_err = format!("attempt {attempt}: {err}");
                eprintln!("backend failed to start on port {port} ({err}); retrying");
            }
        }
    }
    Err(format!("could not start the backend — {last_err}"))
}

/// Drain the child's output so its pipe never fills and blocks it.
fn drain_output(mut rx: tauri::async_runtime::Receiver<tauri_plugin_shell::process::CommandEvent>) {
    tauri::async_runtime::spawn(async move {
        use tauri_plugin_shell::process::CommandEvent;
        while let Some(event) = rx.recv().await {
            if let CommandEvent::Stderr(line) | CommandEvent::Stdout(line) = event {
                eprintln!("[backend] {}", String::from_utf8_lossy(&line).trim_end());
            }
        }
    });
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

/// Raise an OS notification for a critical hotlist item.
///
/// The frontend decides *what* is new (it holds the hotlist and remembers what it
/// has already alerted on); the shell only delivers. Driving the plugin from Rust
/// keeps the webview from needing the notification plugin's own JS permissions.
#[tauri::command]
fn notify_critical(app: tauri::AppHandle, title: String, body: String) -> Result<(), String> {
    app.notification()
        .builder()
        .title(title)
        .body(body)
        .show()
        .map_err(|e| e.to_string())
}

/// Whether Osprey starts (in the tray) when the user signs in to the computer.
#[tauri::command]
fn autostart_enabled(app: tauri::AppHandle) -> Result<bool, String> {
    app.autolaunch().is_enabled().map_err(|e| e.to_string())
}

/// Turn start-at-login on or off. Returns the state the OS actually reports, so the
/// UI never shows a toggle the OS did not honour.
#[tauri::command]
fn set_autostart(app: tauri::AppHandle, enabled: bool) -> Result<bool, String> {
    let launcher = app.autolaunch();
    let changed = if enabled {
        launcher.enable()
    } else {
        launcher.disable()
    };
    changed.map_err(|e| e.to_string())?;
    launcher.is_enabled().map_err(|e| e.to_string())
}

fn show_main_window(app: &tauri::AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let _ = w.unminimize();
        let _ = w.set_focus();
    }
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
    // Optional scopes the admin explicitly opted into (e.g. ACC `data:write` for
    // webhooks). The backend refuses any the connector does not offer.
    optional_scopes: Option<Vec<String>>,
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
            "optional_scopes": optional_scopes.unwrap_or_default(),
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

/// Sign in through the org's identity provider, in the user's own browser.
///
/// Same loopback shape as `oauth_connect`, and deliberately so: the browser is
/// where the user can see the provider's real address bar and certificate, which
/// an embedded webview would hide. The difference is that this one runs *before*
/// there is a session -- it is how the session gets created -- so it takes the
/// backend URL as an argument rather than reading it from shared state.
#[tauri::command]
async fn sso_login(
    session: State<'_, Session>,
    base_url: String,
) -> Result<serde_json::Value, String> {
    if base_url.is_empty() {
        return Err("no backend URL".into());
    }

    // 1) Bind a loopback listener; its port defines the redirect URI. RFC 8252:
    //    a native app registers http://127.0.0.1 with a wildcard port.
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .map_err(|e| format!("bind failed: {e}"))?;
    let port = listener.local_addr().map_err(|e| e.to_string())?.port();
    let redirect_uri = format!("http://127.0.0.1:{port}/callback");

    let http = reqwest::Client::new();

    // 2) Ask the backend to start the flow (it holds the client secret and PKCE).
    let start: AuthorizeResp = http
        .post(format!("{base_url}/auth/sso/start"))
        .json(&serde_json::json!({ "redirect_uri": redirect_uri }))
        .send()
        .await
        .map_err(|e| format!("SSO start failed: {e}"))?
        .error_for_status()
        .map_err(|e| format!("SSO is not available on this server: {e}"))?
        .json()
        .await
        .map_err(|e| e.to_string())?;

    // 3) The provider's consent page, in the system browser.
    open_url(&start.authorize_url);

    // 4) Wait for the redirect back with ?code=...&state=...
    let (code, state) = wait_for_code(listener).await?;
    if state != start.state {
        return Err("state mismatch — possible CSRF; aborting".into());
    }

    // 5) Relay to the backend, which verifies the ID token and mints our session.
    let tokens: serde_json::Value = http
        .post(format!("{base_url}/auth/sso/callback"))
        .json(&serde_json::json!({ "code": code, "state": state }))
        .send()
        .await
        .map_err(|e| format!("SSO callback failed: {e}"))?
        .error_for_status()
        .map_err(|e| format!("sign-in was refused: {e}"))?
        .json()
        .await
        .map_err(|e| e.to_string())?;

    // Adopt the session immediately so a connector OAuth flow can follow without
    // a second round trip through the frontend.
    if let Some(access) = tokens.get("access_token").and_then(|v| v.as_str()) {
        *session.base_url.lock().unwrap() = base_url;
        *session.token.lock().unwrap() = access.to_string();
    }

    Ok(tokens)
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
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_autostart::init(
            MacosLauncher::LaunchAgent,
            Some(vec![BACKGROUND_ARG]),
        ))
        .manage(Session::default())
        .manage(Sidecar::default())
        // Closing the window hides it; Osprey keeps watching from the tray. An agent
        // that stops when its window closes is not "always on", and the critical-
        // item alerts it exists to deliver would silently stop. Quit is in the tray.
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .setup(|app| {
            // Launched at login: stay in the tray until the user asks for the window.
            if std::env::args().any(|arg| arg == BACKGROUND_ARG) {
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.hide();
                }
            }

            // Start the bundled backend, then record its URL once it answers so the
            // frontend can pick it up. Non-fatal: without it the app is a viewer.
            match spawn_backend(app.handle()) {
                Ok(url) => {
                    let handle = app.handle().clone();
                    tauri::async_runtime::spawn(async move {
                        let state = handle.state::<Sidecar>();
                        if wait_until_healthy(&url).await {
                            *state.url.lock().unwrap() = Some(url.clone());
                            *state.status.lock().unwrap() = Some("ready".into());
                            eprintln!("backend ready on {url}");
                        } else {
                            *state.status.lock().unwrap() = Some("unavailable".into());
                            eprintln!("bundled backend never became healthy");
                        }
                    });
                }
                Err(err) => {
                    *app.state::<Sidecar>().status.lock().unwrap() = Some("unavailable".into());
                    eprintln!("no bundled backend ({err}); expecting an external one");
                }
            }

            // System-tray presence — the "always-on" desktop surface.
            let show = MenuItem::with_id(app, "show", "Open Osprey", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show, &quit])?;
            TrayIconBuilder::with_id("osprey-tray")
                .menu(&menu)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "show" => show_main_window(app),
                    "quit" => app.exit(0),
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        show_main_window(tray.app_handle());
                    }
                })
                .build(app)?;
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            set_session,
            oauth_connect,
            sso_login,
            backend_status,
            notify_critical,
            autostart_enabled,
            set_autostart
        ])
        .build(tauri::generate_context!())
        .expect("error while running Osprey")
        .run(|app, event| {
            // Terminate the bundled backend on the way out, so no orphan keeps the
            // port and the SQLite file locked.
            if let tauri::RunEvent::Exit = event {
                app.state::<Sidecar>().shutdown();
            }
        });
}
