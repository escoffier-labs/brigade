//! Read-only client for the local Code Search API (`POST /api/search`). Feature: `codesearch`.

use std::time::Duration;

use anyhow::{Context, Result};
use serde::Deserialize;

use crate::query::blend::ExternalHit;

#[derive(Deserialize)]
struct CodeSearchResult {
    file_path: String,
    score: f64,
}

#[derive(Deserialize)]
struct CodeSearchResponse {
    results: Vec<CodeSearchResult>,
}

/// Client for the Code Search API. URL and key come from the environment.
pub struct CodeSearchClient {
    pub base_url: String,
    pub api_key: Option<String>,
}

const REQUEST_TIMEOUT: Duration = Duration::from_secs(3);

impl Default for CodeSearchClient {
    fn default() -> Self {
        Self::from_env()
    }
}

impl CodeSearchClient {
    pub fn from_env() -> Self {
        Self {
            base_url: std::env::var("CODE_SEARCH_URL")
                .unwrap_or_else(|_| "http://localhost:5204".to_string()),
            api_key: std::env::var("CODE_SEARCH_API_KEY").ok(),
        }
    }

    /// Run a hybrid semantic search, returning one [`ExternalHit`] per file (best score wins).
    pub fn search(&self, query: &str, limit: usize) -> Result<Vec<ExternalHit>> {
        let url = format!("{}/api/search", self.base_url.trim_end_matches('/'));
        let mut req = ureq::post(&url).set("Content-Type", "application/json");
        if let Some(key) = &self.api_key {
            req = req.set("X-API-Key", key);
        }
        let response = req
            .timeout(REQUEST_TIMEOUT)
            .send_json(serde_json::json!({
                "query": query,
                "limit": limit,
                "mode": "hybrid",
            }))
            .context("code search request failed")?;
        let body: CodeSearchResponse = response
            .into_json()
            .context("failed to decode code search response")?;
        Ok(hits_from_results(body.results))
    }
}

/// Collapse per-chunk results into one hit per file, keeping the best score.
fn hits_from_results(results: Vec<CodeSearchResult>) -> Vec<ExternalHit> {
    let mut best: std::collections::HashMap<String, f64> = std::collections::HashMap::new();
    for result in results {
        let entry = best.entry(result.file_path).or_insert(f64::MIN);
        if result.score > *entry {
            *entry = result.score;
        }
    }
    best.into_iter()
        .map(|(file_path, score)| ExternalHit { file_path, score })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

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
        let mut hits = hits_from_results(resp.results);
        hits.sort_by(|a, b| a.file_path.cmp(&b.file_path));
        assert_eq!(hits.len(), 2);
        assert_eq!(hits[0].file_path, "src/auth.ts");
        assert!((hits[0].score - 0.81).abs() < 1e-9); // best chunk score wins
        assert_eq!(hits[1].file_path, "src/util.ts");
    }
}
