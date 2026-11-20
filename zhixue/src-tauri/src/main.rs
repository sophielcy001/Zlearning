#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::{
    env,
    io::{self, BufRead, BufReader},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Mutex,
    },
    thread,
    time::{Duration, Instant},
};

use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem, Submenu},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    webview::NewWindowResponse,
    AppHandle, Manager, RunEvent, Url, WebviewUrl, WebviewWindowBuilder, WindowEvent,
};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogResult};

const FINISH_TIMER: &str = "结束计时并退出";
const STASH_TIMER: &str = "暂存并退出";

#[derive(serde::Deserialize)]
struct ActiveTimer {
    id: String,
    device: Option<String>,
}

#[derive(serde::Deserialize)]
struct TimerState {
    active: Option<ActiveTimer>,
}

#[derive(serde::Serialize)]
struct TimerCommand<'a> {
    action: &'a str,
    #[serde(rename = "timerId")]
    timer_id: &'a str,
}

struct Session {
    endpoint: Url,
    token: String,
    checking: AtomicBool,
}

impl Session {
    fn from_address(address: &Url) -> Self {
        let endpoint = address.join("/api/timer").expect("fixed endpoint");
        let token = address
            .fragment()
            .unwrap()
            .trim_start_matches("/?session=")
            .to_owned();
        Self {
            endpoint,
            token,
            checking: AtomicBool::new(false),
        }
    }

    fn request(&self, command: Option<TimerCommand<'_>>) -> Result<TimerState, reqwest::Error> {
        let client = reqwest::blocking::Client::builder()
            .timeout(Duration::from_secs(3))
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .build()?;
        let request = match command {
            Some(command) => client.post(self.endpoint.clone()).json(&command),
            None => client.get(self.endpoint.clone()),
        };
        request
            .bearer_auth(&self.token)
            .send()?
            .error_for_status()?
            .json()
    }
}

struct Service(Child);

impl Service {
    fn start(app: &AppHandle) -> Result<(Self, Url), Box<dyn std::error::Error>> {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
        let data = match env::var_os("ZHIXUE_DEMO_DATA") {
            Some(path) => PathBuf::from(path),
            None if cfg!(feature = "desktop-bundle") => app.path().app_local_data_dir()?,
            None => root.join("data/tauri"),
        };
        let mut command = if cfg!(feature = "desktop-bundle") {
            let sidecar = env::current_exe()?.with_file_name(if cfg!(windows) {
                "zhixue-service.exe"
            } else {
                "zhixue-service"
            });
            let mut command = Command::new(&sidecar);
            command.current_dir(sidecar.parent().expect("absolute sidecar path"));
            command
        } else {
            let python = root.join(if cfg!(windows) {
                ".venv/Scripts/python.exe"
            } else {
                ".venv/bin/python"
            });
            let mut command = Command::new(python);
            command.current_dir(root).args(["-m", "backend.launch"]);
            command
        };
        command
            .args(["--port", "0", "--stdio", "--desktop-offline", "--data"])
            .arg(data)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            command.creation_flags(CREATE_NO_WINDOW);
        }
        let mut service = Self(command.spawn()?);
        let (send, receive) = mpsc::sync_channel(1);
        let stdout = service.0.stdout.take().unwrap();
        thread::spawn(move || {
            let mut line = String::new();
            let result = BufReader::new(stdout).read_line(&mut line).map(|_| line);
            let _ = send.send(result);
        });
        let line = receive.recv_timeout(Duration::from_secs(18))??;
        let address = Url::parse(line.trim())?;
        if address.scheme() != "http"
            || address.host_str() != Some("127.0.0.1")
            || address.port().is_none()
            || !address
                .fragment()
                .is_some_and(|fragment| fragment.starts_with("/?session="))
        {
            return Err(io::Error::new(io::ErrorKind::InvalidData, "无效的本地服务会话").into());
        }
        Ok((service, address))
    }
}

impl Drop for Service {
    fn drop(&mut self) {
        drop(self.0.stdin.take());
        let deadline = Instant::now() + Duration::from_secs(6);
        while Instant::now() < deadline {
            match self.0.try_wait() {
                Ok(Some(_)) => return,
                Ok(None) => thread::sleep(Duration::from_millis(35)),
                Err(_) => break,
            }
        }
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn show_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

fn release_page_input(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.eval(r#"(() => {
            const root = document.documentElement;
            if (root?.hasAttribute('data-zhixue-quit-pending')) {
                root.removeAttribute('data-zhixue-quit-pending');
                root.inert = false;
            }
        })()"#);
    }
}

fn page_can_quit(app: &AppHandle, lock_input: bool) -> bool {
    let Some(window) = app.get_webview_window("main") else {
        return true;
    };
    // Reuse the editor's unload veto and suspend new edits while committing the timer.
    let script = format!(
        r#"(() => {{
            if (!window.dispatchEvent(new Event('beforeunload', {{ cancelable: true }}))) return false;
            const root = document.documentElement;
            if ({lock_input} && root && !root.inert) {{
                root.setAttribute('data-zhixue-quit-pending', '');
                root.inert = true;
            }}
            return true;
        }})()"#
    );
    let (send, receive) = mpsc::channel();
    if window
        .eval_with_callback(script, move |result| {
            let _ = send.send(result);
        })
        .is_ok()
        && receive
            .recv_timeout(Duration::from_secs(3))
            .is_ok_and(|result| result == "true")
    {
        return true;
    }

    if lock_input {
        release_page_input(app);
    }
    show_window(app);
    app.dialog()
        .message("有未保存的学习内容。请返回编辑，保存笔记或复习草稿后再退出。")
        .title("智学 · 完全退出")
        .parent(&window)
        .buttons(MessageDialogButtons::OkCustom("返回编辑".into()))
        .blocking_show();
    false
}

fn quit_when_ready(app: AppHandle) {
    if app.state::<Session>().checking.swap(true, Ordering::SeqCst) {
        return;
    }
    thread::spawn(move || {
        let session = app.state::<Session>();
        let result = (|| -> Result<bool, reqwest::Error> {
            if !page_can_quit(&app, false) {
                return Ok(false);
            }
            let Some(timer) = session
                .request(None)?
                .active
                .filter(|timer| timer.device.as_deref() != Some("phone"))
            else {
                return Ok(page_can_quit(&app, true));
            };
            let mut dialog = app
                .dialog()
                .message("还有一条学习计时尚未结束。结束后保存有效时长，暂存后可在下次启动时核对。")
                .title("智学 · 完全退出")
                .buttons(MessageDialogButtons::YesNoCancelCustom(
                    FINISH_TIMER.into(),
                    STASH_TIMER.into(),
                    "取消".into(),
                ));
            if let Some(window) = app.get_webview_window("main") {
                if window.is_visible().unwrap_or(false) {
                    dialog = dialog.parent(&window);
                }
            }
            let choice = dialog.blocking_show_with_result();
            let action = match choice {
                MessageDialogResult::Custom(label) if label == FINISH_TIMER => "finish",
                MessageDialogResult::Custom(label) if label == STASH_TIMER => "stash",
                MessageDialogResult::Yes => "finish",
                MessageDialogResult::No => "stash",
                _ => return Ok(false),
            };
            if !page_can_quit(&app, true) {
                return Ok(false);
            }
            session.request(Some(TimerCommand {
                action,
                timer_id: &timer.id,
            }))?;
            Ok(true)
        })();
        match result {
            Ok(true) => app.exit(0),
            Ok(false) => (),
            Err(_) => {
                let service = app.state::<Mutex<Option<Service>>>();
                let exited = service
                    .lock()
                    .ok()
                    .and_then(|mut current| current.as_mut()?.0.try_wait().ok().flatten())
                    .is_some();
                if !exited {
                    release_page_input(&app);
                    app.dialog()
                        .message("计时状态暂时不可用，请稍后重试。")
                        .title("智学 · 计时保存失败")
                        .show(|_| {});
                } else if page_can_quit(&app, true) {
                    app.exit(0);
                }
            }
        }
        session.checking.store(false, Ordering::SeqCst);
    });
}

fn main() -> tauri::Result<()> {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _, _| {
            show_window(app)
        }))
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            let (service, address) = Service::start(app.handle())?;
            app.manage(Mutex::new(Some(service)));
            app.manage(Session::from_address(&address));
            let origin = address.origin();
            let shortcut = if cfg!(target_os = "macos") {
                "CmdOrCtrl+Q"
            } else {
                "Ctrl+Shift+Q"
            };
            let exit = MenuItem::with_id(app, "quit", "完全退出", true, Some(shortcut))?;
            let application = Submenu::with_items(app, "应用", true, &[&exit])?;
            let window_menu = Menu::with_items(app, &[&application])?;
            #[cfg(target_os = "macos")]
            app.set_menu(window_menu.clone())?;
            WebviewWindowBuilder::new(app, "main", WebviewUrl::External(address))
                .title("智学 · Tauri")
                .inner_size(1250.0, 880.0)
                .min_inner_size(720.0, 540.0)
                .menu(window_menu)
                .devtools(false)
                .initialization_script("Object.defineProperty(window, 'zhixueTauriOffline', { value: true });")
                .on_navigation(move |target| target.origin() == origin)
                .on_new_window(|target, _| {
                    if matches!(target.scheme(), "http" | "https") {
                        let _ = open::that_detached(target.as_str());
                    }
                    NewWindowResponse::Deny
                })
                .build()?;

            let show = MenuItem::with_id(app, "show", "打开智学", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "完全退出", true, None::<&str>)?;
            let separator = PredefinedMenuItem::separator(app)?;
            let menu = Menu::with_items(app, &[&show, &separator, &quit])?;
            let icon = app
                .default_window_icon()
                .cloned()
                .ok_or_else(|| io::Error::other("缺少桌面图标"))?;
            TrayIconBuilder::new()
                .icon(icon)
                .tooltip("智学 · Tauri")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_tray_icon_event(|tray, event| {
                    if matches!(
                        event,
                        TrayIconEvent::Click {
                            button: MouseButton::Left,
                            button_state: MouseButtonState::Up,
                            ..
                        }
                    ) {
                        show_window(tray.app_handle());
                    }
                })
                .build(app)?;
            Ok(())
        })
        .on_menu_event(|app, event| match event.id().as_ref() {
            "show" => show_window(app),
            "quit" => quit_when_ready(app.clone()),
            _ => {}
        })
        .on_window_event(|window, event| {
            let hidden = match event {
                WindowEvent::CloseRequested { api, .. } => {
                    api.prevent_close();
                    let _ = window.hide();
                    true
                }
                WindowEvent::Resized(_) | WindowEvent::Focused(false) => {
                    window.is_minimized().unwrap_or(false)
                }
                _ => false,
            };
            // A capturing WebView2 document can stay visible after the native window hides.
            if hidden {
                if let Some(view) = window.app_handle().get_webview_window(window.label()) {
                    let _ = view.eval("window.dispatchEvent(new Event('zhixue:window-hidden'))");
                }
            }
        })
        .build(tauri::generate_context!())?;

    app.run(|handle, event| match event {
        RunEvent::ExitRequested {
            code: None, api, ..
        } => {
            api.prevent_exit();
            quit_when_ready(handle.clone());
        }
        RunEvent::Exit => {
            if let Some(service) = handle.try_state::<Mutex<Option<Service>>>() {
                if let Ok(mut process) = service.lock() {
                    process.take();
                }
            }
        }
        _ => {}
    });
    Ok(())
}
