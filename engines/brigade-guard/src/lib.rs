//! Shared PCRE2 compiler for the CLI and hub. No policy or text in errors.
pub use pcre2::bytes::Regex;
use pcre2::bytes::RegexBuilder;
use std::ffi::c_void;

pub fn compile(pattern: &str, flags: u32, utf: bool) -> Result<Regex, Box<dyn std::error::Error>> {
    if flags & !(2 | 8 | 16 | 32 | 64 | 256) != 0 || !pattern.is_ascii() {
        return Err("unsupported Python regex flags or pattern".into());
    }
    if utf && flags & 256 != 0 {
        return Err("ASCII flag is unsupported in UTF mode".into());
    }
    Ok(RegexBuilder::new()
        .caseless(flags & 2 != 0)
        .multi_line(flags & 8 != 0)
        .dotall(flags & 16 != 0)
        .extended(flags & 64 != 0)
        .utf(utf)
        .ucp(utf)
        .jit(true)
        .build(pattern)?)
}

pub fn config_string(what: u32) -> Result<String, Box<dyn std::error::Error>> {
    let mut buffer = [0u8; 64];
    // SAFETY: version options write at most 24 NUL-terminated code units.
    let used = unsafe { pcre2_sys::pcre2_config_8(what, buffer.as_mut_ptr() as *mut c_void) };
    if used <= 0 || used as usize > buffer.len() {
        return Err("PCRE2 config unavailable".into());
    }
    Ok(std::str::from_utf8(&buffer[..used as usize - 1])?.to_owned())
}

pub fn pinned() -> bool {
    config_string(pcre2_sys::PCRE2_CONFIG_VERSION).is_ok_and(|s| s == "10.46 2025-08-27")
        && config_string(pcre2_sys::PCRE2_CONFIG_UNICODE_VERSION).is_ok_and(|s| s == "16.0.0")
}
