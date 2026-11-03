//! Startup failures must be refused before the Rust listener exists.
use std::{net::Ipv4Addr, process::Command};
fn hub(args: &[&str], environment: &[(&str, &str)]) -> std::process::Output {
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_brigade-hub"));
    cmd.env_remove("BRIGADE_FLEET_TOKEN")
        .env_remove("BRIGADE_FLEET_DECK_CONFIG");
    cmd.args(args);
    for (key, value) in environment {
        cmd.env(key, value);
    }
    cmd.output().unwrap()
}
#[test]
fn missing_host_and_token() {
    assert_eq!(hub(&[], &[]).status.code(), Some(2));
    let host = Ipv4Addr::LOCALHOST.to_string();
    let out = hub(&["--host", &host], &[]);
    assert_eq!(out.status.code(), Some(2));
    assert!(
        String::from_utf8_lossy(&out.stderr)
            .contains("no fleet hub token: set BRIGADE_FLEET_TOKEN or pass --token-file")
    );
}
#[test]
fn config_rejected_by_python_before_listening() {
    let host = Ipv4Addr::LOCALHOST.to_string();
    for (args, env, expected) in [
        (
            vec![
                "--host",
                host.as_str(),
                "--deck-config",
                "/missing/fake-deck.json",
            ],
            vec![("BRIGADE_FLEET_TOKEN", "fake-test-token")],
            "deck",
        ),
        (
            vec![
                "--host",
                host.as_str(),
                "--token-file",
                "/missing/fake-token",
            ],
            vec![],
            "could not read token file",
        ),
        (
            vec!["--host", host.as_str()],
            vec![
                ("BRIGADE_FLEET_TOKEN", "fake-test-token"),
                ("BRIGADE_FLEET_MESSAGES_REQUIRE_SEALED", "invalid"),
            ],
            "BRIGADE_FLEET_MESSAGES_REQUIRE_SEALED",
        ),
    ] {
        for external in [false, true] {
            let mut args = args.clone();
            let upstream = format!("http://{}:1", Ipv4Addr::LOCALHOST);
            if external {
                args.extend(["--upstream", upstream.as_str()]);
            }
            let out = hub(&args, &env);
            assert_eq!(out.status.code(), Some(2));
            assert!(
                String::from_utf8_lossy(&out.stderr).contains(expected),
                "{}",
                String::from_utf8_lossy(&out.stderr)
            );
        }
    }
}
