//! Read-only client for the local Code Search API (`POST /api/search`). Feature: `codesearch`.

use std::io::Read;
use std::path::{Path, PathBuf};
use std::time::Duration;

use anyhow::{Context, Result};
use serde::Deserialize;
use serde_json::{Map, Value, json};

use crate::query::blend::ExternalHit;

#[derive(Deserialize)]
struct CodeSearchResult {
    file_path: String,
    score: f64,
    #[allow(dead_code)]
    project: Option<String>,
}

#[derive(Deserialize)]
struct CodeSearchResponse {
    results: Vec<CodeSearchResult>,
}

#[derive(Clone, Debug)]
struct ManifestRepoMatch {
    project: String,
    file_prefix: String,
}

#[derive(Deserialize)]
struct CodeSearchManifest {
    semantic_api_url: Option<String>,
    repos: Vec<CodeSearchManifestRepo>,
}

#[derive(Deserialize)]
struct CodeSearchManifestRepo {
    repo_root: PathBuf,
    code_search_project: String,
    code_search_file_prefix: String,
}

/// Client for the Code Search API. URL and key come from the environment.
pub struct CodeSearchClient {
    pub base_url: String,
    pub api_key: Option<String>,
    repo_match: Option<ManifestRepoMatch>,
}

const DEFAULT_REQUEST_TIMEOUT_SECS: u64 = 20;
const MAX_CODE_SEARCH_RESPONSE_BYTES: usize = 8 * 1024 * 1024;

/// Read timeout for search requests. A busy indexer can take many seconds to
/// answer, so this is tunable via `CODE_SEARCH_TIMEOUT_SECS`. Connection
/// failures to an unreachable service still error immediately.
fn request_timeout() -> Duration {
    let secs = std::env::var("CODE_SEARCH_TIMEOUT_SECS")
        .ok()
        .and_then(|value| value.parse::<u64>().ok())
        .filter(|secs| *secs > 0)
        .unwrap_or(DEFAULT_REQUEST_TIMEOUT_SECS);
    Duration::from_secs(secs)
}

impl Default for CodeSearchClient {
    fn default() -> Self {
        Self::from_env()
    }
}

impl CodeSearchClient {
    pub fn from_env() -> Self {
        Self::from_env_for_repo(None)
    }

    pub fn from_env_for_repo(repo_root: Option<&Path>) -> Self {
        let manifest = CodeSearchManifest::discover().ok().flatten();
        let repo_match = manifest
            .as_ref()
            .and_then(|manifest| repo_root.and_then(|repo_root| manifest.match_repo(repo_root)));
        let manifest_url = manifest.and_then(|manifest| manifest.semantic_api_url);
        Self {
            base_url: std::env::var("CODE_SEARCH_URL")
                .ok()
                .or(manifest_url)
                .unwrap_or_else(|| "http://localhost:5204".to_string()),
            api_key: std::env::var("CODE_SEARCH_API_KEY").ok(),
            repo_match,
        }
    }

    /// Run a hybrid semantic search, returning one [`ExternalHit`] per file (best score wins).
    pub fn search(&self, query: &str, limit: usize) -> Result<Vec<ExternalHit>> {
        let url = format!("{}/api/search", self.base_url.trim_end_matches('/'));
        let mut req = ureq::post(&url).set("Content-Type", "application/json");
        if let Some(key) = &self.api_key {
            req = req.set("X-API-Key", key);
        }
        let mut payload = Map::new();
        payload.insert("query".to_string(), json!(query));
        payload.insert("limit".to_string(), json!(limit));
        payload.insert("mode".to_string(), json!("hybrid"));
        if let Some(repo_match) = &self.repo_match {
            payload.insert("project".to_string(), json!(repo_match.project));
        }
        let response = req
            .timeout(request_timeout())
            .send_json(Value::Object(payload))
            .context("code search request failed")?;
        let mut bytes = Vec::new();
        response
            .into_reader()
            .take((MAX_CODE_SEARCH_RESPONSE_BYTES + 1) as u64)
            .read_to_end(&mut bytes)
            .context("failed to read code search response")?;
        if bytes.len() > MAX_CODE_SEARCH_RESPONSE_BYTES {
            anyhow::bail!(
                "code search response exceeds {MAX_CODE_SEARCH_RESPONSE_BYTES} byte limit"
            );
        }
        let body: CodeSearchResponse =
            serde_json::from_slice(&bytes).context("failed to decode code search response")?;
        Ok(hits_from_results(body.results, self.repo_match.as_ref()))
    }
}

/// Collapse per-chunk results into one hit per file, keeping the best score.
fn hits_from_results(
    results: Vec<CodeSearchResult>,
    repo_match: Option<&ManifestRepoMatch>,
) -> Vec<ExternalHit> {
    let mut best: std::collections::HashMap<String, f64> = std::collections::HashMap::new();
    for result in results {
        let Some(file_path) = normalize_result_path(&result.file_path, repo_match) else {
            continue;
        };
        let entry = best.entry(file_path).or_insert(f64::MIN);
        if result.score > *entry {
            *entry = result.score;
        }
    }
    best.into_iter()
        .map(|(file_path, score)| ExternalHit { file_path, score })
        .collect()
}

fn normalize_result_path(
    file_path: &str,
    repo_match: Option<&ManifestRepoMatch>,
) -> Option<String> {
    let Some(repo_match) = repo_match else {
        return Some(file_path.to_string());
    };
    if repo_match.file_prefix.is_empty() {
        return Some(file_path.to_string());
    }
    file_path
        .strip_prefix(&repo_match.file_prefix)
        .map(|path| path.trim_start_matches('/').to_string())
        .filter(|path| !path.is_empty())
}

impl CodeSearchManifest {
    fn discover() -> Result<Option<Self>> {
        let Some(path) = manifest_path() else {
            return Ok(None);
        };
        Self::read(&path)
    }

    fn read(path: &Path) -> Result<Option<Self>> {
        if !path.exists() {
            return Ok(None);
        }
        let text = std::fs::read_to_string(path)
            .with_context(|| format!("failed to read {}", path.display()))?;
        Ok(Some(serde_json::from_str(&text).with_context(|| {
            format!("failed to parse {}", path.display())
        })?))
    }

    fn match_repo(&self, repo_root: &Path) -> Option<ManifestRepoMatch> {
        let repo_root = repo_root.canonicalize().ok()?;
        self.repos.iter().find_map(|repo| {
            let manifest_root = repo.repo_root.canonicalize().ok()?;
            if manifest_root == repo_root {
                Some(ManifestRepoMatch {
                    project: repo.code_search_project.clone(),
                    file_prefix: repo.code_search_file_prefix.clone(),
                })
            } else {
                None
            }
        })
    }
}

fn manifest_path() -> Option<PathBuf> {
    if let Some(path) = std::env::var_os("CODE_INDEX_MANIFEST") {
        return Some(PathBuf::from(path));
    }
    if let Some(data_home) = std::env::var_os("XDG_DATA_HOME") {
        return Some(PathBuf::from(data_home).join("code-index/manifest.json"));
    }
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .map(|home| home.join(".local/share/code-index/manifest.json"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::path::Path;
    use std::sync::Mutex;
    use std::thread::{self, JoinHandle};
    use std::time::{Duration, Instant};

    static ENV_LOCK: Mutex<()> = Mutex::new(());

    #[test]
    fn parses_documented_response_and_dedupes_by_file() {
        // Shape per the Code Search API contract: results[] with file_path + score.
        let json = r#"{
            "results": [
                {"file_path": "src/auth.ts", "score": 0.62, "chunk_index": 0},
                {"file_path": "src/auth.ts", "score": 0.81, "chunk_index": 2},
                {"file_path": "src/util.ts", "score": 0.40, "chunk_index": 0}
            ],
            "total_matches": 3,
            "mode": "hybrid"
        }"#;
        let resp: CodeSearchResponse = serde_json::from_str(json).unwrap();
        let mut hits = hits_from_results(resp.results, None);
        hits.sort_by(|a, b| a.file_path.cmp(&b.file_path));
        assert_eq!(hits.len(), 2);
        assert_eq!(hits[0].file_path, "src/auth.ts");
        assert!((hits[0].score - 0.81).abs() < 1e-9); // best chunk score wins
        assert_eq!(hits[1].file_path, "src/util.ts");
    }

    #[test]
    fn parses_manifest_and_matches_canonical_repo_root() {
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path().join("repo");
        std::fs::create_dir(&repo).unwrap();
        let uncanonical_repo = repo.join("..").join("repo");
        let manifest_path = dir.path().join("manifest.json");
        std::fs::write(
            &manifest_path,
            format!(
                r#"{{
                    "version": 1,
                    "semantic_api_url": "http://semantic.example",
                    "repos": [{{
                        "repo_root": "{}",
                        "code_search_project": "repos/demo",
                        "code_search_file_prefix": "repos/demo/"
                    }}]
                }}"#,
                repo.display()
            ),
        )
        .unwrap();

        let manifest = CodeSearchManifest::read(&manifest_path).unwrap().unwrap();
        let matched = manifest.match_repo(&uncanonical_repo).unwrap();

        assert_eq!(
            manifest.semantic_api_url.as_deref(),
            Some("http://semantic.example")
        );
        assert_eq!(matched.project, "repos/demo");
        assert_eq!(matched.file_prefix, "repos/demo/");
    }

    #[test]
    fn manifest_url_falls_back_but_code_search_url_takes_precedence() {
        let _guard = ENV_LOCK.lock().unwrap();
        let dir = tempfile::tempdir().unwrap();
        let manifest_path = dir.path().join("manifest.json");
        std::fs::write(
            &manifest_path,
            r#"{"version":1,"semantic_api_url":"http://manifest.example","repos":[]}"#,
        )
        .unwrap();

        unsafe {
            std::env::remove_var("CODE_SEARCH_URL");
            std::env::set_var("CODE_INDEX_MANIFEST", &manifest_path);
        }
        let client = CodeSearchClient::from_env_for_repo(None);
        assert_eq!(client.base_url, "http://manifest.example");

        unsafe {
            std::env::set_var("CODE_SEARCH_URL", "http://env.example");
        }
        let client = CodeSearchClient::from_env_for_repo(None);
        assert_eq!(client.base_url, "http://env.example");

        unsafe {
            std::env::remove_var("CODE_SEARCH_URL");
            std::env::remove_var("CODE_INDEX_MANIFEST");
        }
    }

    #[test]
    fn request_body_scopes_project_and_strips_matching_prefix() {
        let _guard = ENV_LOCK.lock().unwrap();
        let dir = tempfile::tempdir().unwrap();
        let repo = dir.path().join("repo");
        std::fs::create_dir(&repo).unwrap();
        let manifest_path = write_manifest(
            dir.path(),
            &repo,
            "http://manifest.example",
            "repos/demo",
            "repos/demo/",
        );
        let mock = MockCodeSearch::new(
            "needle",
            Some("repos/demo"),
            r#"{"results":[
                {"file_path":"repos/demo/src/lib.rs","score":0.5,"project":"repos/demo"},
                {"file_path":"other/src/lib.rs","score":0.9,"project":"other"},
                {"file_path":"repos/demo/src/lib.rs","score":0.7,"project":"repos/demo"}
            ]}"#,
        );

        unsafe {
            std::env::set_var("CODE_SEARCH_URL", &mock.base_url);
            std::env::set_var("CODE_INDEX_MANIFEST", &manifest_path);
            std::env::remove_var("CODE_SEARCH_API_KEY");
        }
        let client = CodeSearchClient::from_env_for_repo(Some(&repo));
        let mut hits = client.search("needle", 5).unwrap();
        mock.join();
        unsafe {
            std::env::remove_var("CODE_SEARCH_URL");
            std::env::remove_var("CODE_INDEX_MANIFEST");
        }

        hits.sort_by(|a, b| a.file_path.cmp(&b.file_path));
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].file_path, "src/lib.rs");
        assert!((hits[0].score - 0.7).abs() < 1e-9);
    }

    #[test]
    fn no_manifest_preserves_unscoped_legacy_request_and_paths() {
        let _guard = ENV_LOCK.lock().unwrap();
        let mock = MockCodeSearch::new(
            "needle",
            None,
            r#"{"results":[{"file_path":"repos/demo/src/lib.rs","score":0.5}]}"#,
        );

        unsafe {
            std::env::set_var("CODE_SEARCH_URL", &mock.base_url);
            std::env::set_var(
                "CODE_INDEX_MANIFEST",
                "/tmp/graphtrail-missing-manifest.json",
            );
            std::env::remove_var("CODE_SEARCH_API_KEY");
        }
        let client = CodeSearchClient::from_env_for_repo(None);
        let hits = client.search("needle", 5).unwrap();
        mock.join();
        unsafe {
            std::env::remove_var("CODE_SEARCH_URL");
            std::env::remove_var("CODE_INDEX_MANIFEST");
        }

        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].file_path, "repos/demo/src/lib.rs");
    }

    #[test]
    fn oversized_response_is_rejected_before_json_decode() {
        let _guard = ENV_LOCK.lock().unwrap();
        let body = format!(
            r#"{{"results":[]}}{}"#,
            " ".repeat(MAX_CODE_SEARCH_RESPONSE_BYTES + 1)
        );
        let mock = MockCodeSearch::new("needle", None, body);

        unsafe {
            std::env::set_var("CODE_SEARCH_URL", &mock.base_url);
            std::env::set_var(
                "CODE_INDEX_MANIFEST",
                "/tmp/graphtrail-missing-manifest.json",
            );
            std::env::remove_var("CODE_SEARCH_API_KEY");
        }
        let client = CodeSearchClient::from_env_for_repo(None);
        let result = client.search("needle", 5);
        mock.join();
        unsafe {
            std::env::remove_var("CODE_SEARCH_URL");
            std::env::remove_var("CODE_INDEX_MANIFEST");
        }

        let error = result.expect_err("oversized response must be rejected");
        let expected =
            format!("code search response exceeds {MAX_CODE_SEARCH_RESPONSE_BYTES} byte limit");
        assert!(
            format!("{error:#}").contains(&expected),
            "unexpected error: {error:#}"
        );
    }

    fn write_manifest(
        dir: &Path,
        repo: &Path,
        api_url: &str,
        project: &str,
        prefix: &str,
    ) -> std::path::PathBuf {
        let manifest_path = dir.join("manifest.json");
        std::fs::write(
            &manifest_path,
            format!(
                r#"{{
                    "version": 1,
                    "semantic_api_url": "{api_url}",
                    "repos": [{{
                        "repo_root": "{}",
                        "code_search_project": "{project}",
                        "code_search_file_prefix": "{prefix}"
                    }}]
                }}"#,
                repo.display()
            ),
        )
        .unwrap();
        manifest_path
    }

    struct MockCodeSearch {
        base_url: String,
        handle: JoinHandle<()>,
    }

    impl MockCodeSearch {
        fn new(
            expected_query: &'static str,
            expected_project: Option<&'static str>,
            body: impl Into<String>,
        ) -> Self {
            let body = body.into();
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            listener.set_nonblocking(true).unwrap();
            let base_url = format!("http://{}", listener.local_addr().unwrap());
            let handle = thread::spawn(move || {
                let deadline = Instant::now() + Duration::from_secs(2);
                let (mut stream, _) = loop {
                    match listener.accept() {
                        Ok(accepted) => break accepted,
                        Err(err) if err.kind() == std::io::ErrorKind::WouldBlock => {
                            assert!(Instant::now() < deadline, "mock code search was not called");
                            thread::sleep(Duration::from_millis(10));
                        }
                        Err(err) => panic!("mock code search accept failed: {err}"),
                    }
                };
                let request = read_request(&mut stream);
                let text = String::from_utf8_lossy(&request);
                assert!(text.starts_with("POST /api/search "));
                let body_start = text.find("\r\n\r\n").unwrap() + 4;
                let payload: serde_json::Value = serde_json::from_str(&text[body_start..]).unwrap();
                assert_eq!(payload["query"], expected_query);
                assert_eq!(payload["mode"], "hybrid");
                match expected_project {
                    Some(project) => assert_eq!(payload["project"], project),
                    None => assert!(payload.get("project").is_none()),
                }
                let response = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    body.len(),
                    body
                );
                stream.write_all(response.as_bytes()).unwrap();
            });
            Self { base_url, handle }
        }

        fn join(self) {
            self.handle.join().unwrap();
        }
    }

    fn read_request(stream: &mut std::net::TcpStream) -> Vec<u8> {
        let mut request = Vec::new();
        let mut buf = [0_u8; 1024];
        loop {
            let read = stream.read(&mut buf).unwrap();
            assert!(read > 0, "mock code search request ended early");
            request.extend_from_slice(&buf[..read]);
            if request_complete(&request) {
                break;
            }
        }
        request
    }

    fn request_complete(request: &[u8]) -> bool {
        let Some(header_end) = request.windows(4).position(|window| window == b"\r\n\r\n") else {
            return false;
        };
        let headers = String::from_utf8_lossy(&request[..header_end]);
        let content_length = headers
            .lines()
            .find_map(|line| {
                let (name, value) = line.split_once(':')?;
                if name.eq_ignore_ascii_case("content-length") {
                    value.trim().parse::<usize>().ok()
                } else {
                    None
                }
            })
            .unwrap_or(0);
        request.len() >= header_end + 4 + content_length
    }
}
