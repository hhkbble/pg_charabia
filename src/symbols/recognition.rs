//! Raw ASCII recognition. Failed envelopes remain ordinary parser input.
use std::net::{Ipv4Addr, Ipv6Addr};

use iri_string::types::UriStr;
use linkify::{LinkFinder, LinkKind};
use unicode_normalization::char::is_combining_mark;

use super::{Candidate, SymbolKind};
use crate::prepare::MAX_RAW_TOKEN_BYTES;

#[derive(Debug, Clone, Copy)]
pub(crate) struct HostParts<'a> {
    pub host: &'a str,
    pub port: Option<&'a str>,
    pub is_ip: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Numword<'a> {
    Literal,
    Mixed,
    Hex(&'a str),
}

#[derive(Debug)]
pub(crate) enum Parsed<'a> {
    Url {
        uri: &'a UriStr,
        recall: Option<&'a UriStr>,
    },
    Host(HostParts<'a>),
    Email {
        local: &'a str,
        domain: &'a str,
    },
    File(&'a str),
    Numword(Numword<'a>),
}

/// Validate the actual dictionary input, including calls that bypass the parser.
pub(crate) fn parse(kind: SymbolKind, raw: &str) -> Option<Parsed<'_>> {
    if raw.is_empty() || raw.len() > MAX_RAW_TOKEN_BYTES || !raw.is_ascii() {
        return None;
    }
    match kind {
        SymbolKind::Url => {
            let uri = url(raw)?;
            let recall = recall_url(raw);
            Some(Parsed::Url { uri, recall })
        }
        SymbolKind::Host => host(raw, false).map(Parsed::Host),
        SymbolKind::Email => {
            let (local, domain) = email(raw)?;
            Some(Parsed::Email { local, domain })
        }
        SymbolKind::File => file(raw).then_some(Parsed::File(raw)),
        SymbolKind::Numword => numword(raw).map(Parsed::Numword),
    }
}

/// Host grammar deliberately has no DNS, public-suffix, or IDNA dependency.
pub(crate) fn host(raw: &str, single_label: bool) -> Option<HostParts<'_>> {
    if raw.is_empty() || !raw.is_ascii() {
        return None;
    }
    let (name, port) = if raw.starts_with('[') {
        let end = raw.find(']')? + 1;
        let tail = &raw[end..];
        let port = if tail.is_empty() {
            None
        } else {
            Some(tail.strip_prefix(':')?)
        };
        (&raw[..end], port)
    } else if let Some((name, port)) = raw.rsplit_once(':') {
        if name.contains(':') {
            return None;
        }
        (name, Some(port))
    } else {
        (raw, None)
    };
    if port.is_some_and(|port| port.is_empty() || !port.bytes().all(|b| b.is_ascii_digit())) {
        return None;
    }
    let is_ip = if let Some(ip) = name.strip_prefix('[').and_then(|s| s.strip_suffix(']')) {
        ip.parse::<Ipv6Addr>().ok()?;
        true
    } else if name.parse::<Ipv4Addr>().is_ok() {
        true
    } else {
        if name.len() > 253 || name.is_empty() {
            return None;
        }
        let mut labels = name.split('.');
        let first = labels.next()?;
        if !dns_label(first) {
            return None;
        }
        let mut count = 1;
        let mut last = first;
        for label in labels {
            if !dns_label(label) {
                return None;
            }
            count += 1;
            last = label;
        }
        if count == 1 {
            if !single_label {
                return None;
            }
        } else if last.len() < 2 || !last.bytes().all(|b| b.is_ascii_alphabetic()) {
            return None;
        }
        false
    };
    Some(HostParts {
        host: name,
        port,
        is_ip,
    })
}

fn dns_label(raw: &str) -> bool {
    !raw.is_empty()
        && raw.len() <= 63
        && raw.as_bytes()[0].is_ascii_alphanumeric()
        && raw.as_bytes()[raw.len() - 1].is_ascii_alphanumeric()
        && raw.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
}

fn url(raw: &str) -> Option<&UriStr> {
    if !raw.is_ascii() || raw.bytes().any(envelope_end) {
        return None;
    }
    let uri = UriStr::new(raw).ok()?;
    if !raw[uri.scheme_str().len()..].starts_with("://") {
        return None;
    }
    let authority = uri.authority_components()?;
    if authority.host().is_empty()
        && (!uri.scheme_str().eq_ignore_ascii_case("file") || authority.port().is_some())
    {
        return None;
    }
    Some(uri)
}

fn trim_url_suffix(raw: &str, invalid_only: bool) -> usize {
    let mut balance = [0isize; 3];
    for b in raw.bytes() {
        match b {
            b'(' => balance[0] += 1,
            b')' => balance[0] -= 1,
            b'[' => balance[1] += 1,
            b']' => balance[1] -= 1,
            b'{' => balance[2] += 1,
            b'}' => balance[2] -= 1,
            _ => {}
        }
    }
    let mut end = raw.len();
    while let Some(&last) = raw.as_bytes().get(end.wrapping_sub(1)) {
        let closing = match last {
            b')' if !invalid_only => Some(0),
            b']' => Some(1),
            b'}' => Some(2),
            _ => None,
        };
        if let Some(index) = closing.filter(|&index| balance[index] < 0) {
            balance[index] += 1;
            end -= 1;
        } else if !invalid_only && matches!(last, b'.' | b',' | b'!') {
            end -= 1;
        } else {
            break;
        }
    }
    end
}

fn recall_url(raw: &str) -> Option<&UriStr> {
    let end = trim_url_suffix(raw, false);
    (end < raw.len()).then(|| url(&raw[..end])).flatten()
}

fn email(raw: &str) -> Option<(&str, &str)> {
    let (local, domain) = raw.split_once('@')?;
    if local.is_empty()
        || local.starts_with('.')
        || local.ends_with('.')
        || local.contains("..")
        || !local
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'+' | b'-'))
        || host(domain, true)?.port.is_some()
        || domain.starts_with('[')
    {
        return None;
    }
    Some((local, domain))
}

fn explicit_file(raw: &str) -> bool {
    raw.contains('/') || raw.contains('\\')
}

fn file(raw: &str) -> bool {
    if !raw.is_ascii()
        || !raw.bytes().any(|b| b.is_ascii_alphanumeric())
        || raw.bytes().any(|b| envelope_end(b) || b.is_ascii_control())
    {
        return false;
    }
    if raw.contains(':') {
        let bytes = raw.as_bytes();
        if bytes.len() < 3
            || !bytes[0].is_ascii_alphabetic()
            || bytes[1] != b':'
            || !matches!(bytes[2], b'/' | b'\\')
            || raw[2..].contains(':')
        {
            return false;
        }
    }
    if explicit_file(raw) {
        return true;
    }
    if raw.starts_with('.') {
        return raw.len() > 1 && !raw.contains('@');
    }
    raw.bytes().any(|b| b.is_ascii_alphabetic())
        && raw.rsplit_once('.').is_some_and(|(name, suffix)| {
            !name.is_empty()
                && !suffix.is_empty()
                && name.as_bytes()[0].is_ascii_alphanumeric()
                && !name.contains('@')
                && suffix.bytes().all(|b| b.is_ascii_alphanumeric())
        })
}

fn preferred_file(raw: &str) -> bool {
    const SUFFIXES: &[&str] = &[
        "c", "h", "cc", "cpp", "rs", "py", "js", "ts", "tsx", "json", "toml", "yaml", "yml", "md",
        "txt", "log", "sql", "html", "xml", "css", "sh", "wasm", "so", "dll", "exe",
    ];
    raw.rsplit_once('.')
        .is_some_and(|(_, suffix)| SUFFIXES.iter().any(|s| suffix.eq_ignore_ascii_case(s)))
}

fn number(raw: &str) -> bool {
    let bytes = raw.as_bytes();
    let mut i = usize::from(matches!(bytes.first(), Some(b'+' | b'-')));
    let begin = i;
    while bytes.get(i).is_some_and(u8::is_ascii_digit) {
        i += 1;
    }
    let mut digits = i - begin;
    if bytes.get(i) == Some(&b'.') {
        i += 1;
        let begin = i;
        while bytes.get(i).is_some_and(u8::is_ascii_digit) {
            i += 1;
        }
        digits += i - begin;
    }
    if digits == 0 {
        return false;
    }
    if matches!(bytes.get(i), Some(b'e' | b'E')) {
        i += 1;
        if matches!(bytes.get(i), Some(b'+' | b'-')) {
            i += 1;
        }
        let begin = i;
        while bytes.get(i).is_some_and(u8::is_ascii_digit) {
            i += 1;
        }
        if i == begin {
            return false;
        }
    }
    i == bytes.len()
}

fn version(raw: &str) -> bool {
    let raw = raw.strip_prefix('v').unwrap_or(raw);
    let mut count = 0;
    for segment in raw.split('.') {
        if segment.is_empty() || !segment.bytes().all(|b| b.is_ascii_digit()) {
            return false;
        }
        count += 1;
    }
    count >= 3
}

fn numword(raw: &str) -> Option<Numword<'_>> {
    if let Some(body) = raw.strip_prefix("0x").or_else(|| raw.strip_prefix("0X"))
        && !body.is_empty()
        && body.bytes().all(|b| b.is_ascii_hexdigit())
    {
        return Some(Numword::Hex(body));
    }
    if number(raw) || version(raw) {
        return Some(Numword::Literal);
    }
    if (4..=64).contains(&raw.len())
        && raw.bytes().all(|b| b.is_ascii_hexdigit())
        && raw.bytes().any(|b| b.is_ascii_digit())
        && raw.bytes().any(|b| b.is_ascii_alphabetic())
    {
        return Some(Numword::Hex(raw));
    }
    if !raw.bytes().any(|b| b.is_ascii_alphabetic()) {
        return None;
    }
    if raw.bytes().all(|b| b.is_ascii_alphanumeric()) {
        return raw
            .bytes()
            .any(|b| b.is_ascii_digit())
            .then_some(Numword::Mixed);
    }
    (raw.contains(['_', '-'])
        && raw
            .split(['_', '-'])
            .all(|part| !part.is_empty() && part.bytes().all(|b| b.is_ascii_alphanumeric())))
    .then_some(Numword::Mixed)
}

fn envelope_end(b: u8) -> bool {
    b.is_ascii_whitespace() || matches!(b, b'<' | b'>' | b'\'' | b'"' | b'`')
}

fn scheme_char(b: u8) -> bool {
    b.is_ascii_alphanumeric() || matches!(b, b'+' | b'-' | b'.')
}

fn lexical_char(b: u8) -> bool {
    b.is_ascii_alphanumeric()
        || matches!(
            b,
            b'_' | b'-' | b'.' | b'/' | b'\\' | b':' | b'+' | b'@' | b'%' | b'~' | b'[' | b']'
        )
}

fn scalar_envelope(input: &str, start: usize, end: usize) -> bool {
    !input[..start]
        .chars()
        .next_back()
        .is_some_and(is_combining_mark)
        && !input[end..].chars().next().is_some_and(is_combining_mark)
}

fn overlaps(blocked: &[(usize, usize)], start: usize, end: usize) -> bool {
    let i = blocked.partition_point(|&(_, end)| end <= start);
    blocked.get(i).is_some_and(|&(begin, _)| begin < end)
}

fn lexical_kind(raw: &str) -> Option<SymbolKind> {
    if raw.parse::<Ipv4Addr>().is_ok() || raw.starts_with('[') && host(raw, false).is_some() {
        return Some(SymbolKind::Host);
    }
    if matches!(numword(raw), Some(Numword::Literal)) {
        return Some(SymbolKind::Numword);
    }
    if file(raw) && (explicit_file(raw) || raw.starts_with('.') || preferred_file(raw)) {
        return Some(SymbolKind::File);
    }
    if host(raw, false).is_some() {
        return Some(SymbolKind::Host);
    }
    if file(raw) {
        return Some(SymbolKind::File);
    }
    numword(raw).map(|_| SymbolKind::Numword)
}

/// Choose leftmost, longest ASCII structures without deleting failed input.
pub(crate) fn scan(input: &str) -> Vec<Candidate> {
    let mut candidates = Vec::new();
    let mut finder = LinkFinder::new();
    finder
        .kinds(&[LinkKind::Email])
        .email_domain_must_have_dot(false);
    let bytes = input.as_bytes();
    let mut run_start = 0;
    while run_start < bytes.len() {
        if !bytes[run_start].is_ascii() {
            run_start += input[run_start..]
                .chars()
                .next()
                .expect("in-bounds scalar")
                .len_utf8();
            continue;
        }
        let mut run_end = run_start;
        while run_end < bytes.len() && bytes[run_end].is_ascii() {
            run_end += 1;
        }
        scan_run(input, run_start, run_end, &finder, &mut candidates);
        run_start = run_end;
    }
    candidates.sort_by_key(|c| (c.start, std::cmp::Reverse(c.end), priority(c.kind)));
    let mut end = 0;
    candidates.retain(|candidate| {
        if candidate.start < end {
            return false;
        }
        end = candidate.end;
        true
    });
    candidates
}

fn priority(kind: SymbolKind) -> u8 {
    match kind {
        SymbolKind::Url => 0,
        SymbolKind::Email => 1,
        SymbolKind::File => 2,
        SymbolKind::Host => 3,
        SymbolKind::Numword => 4,
    }
}

fn scan_run(
    input: &str,
    run_start: usize,
    run_end: usize,
    finder: &LinkFinder,
    candidates: &mut Vec<Candidate>,
) {
    let run = &input[run_start..run_end];
    let bytes = run.as_bytes();
    let mut blocked = Vec::new();
    let mut i = 0;
    while i < bytes.len() {
        if !bytes[i].is_ascii_alphabetic()
            || i > 0 && (scheme_char(bytes[i - 1]) || bytes[i - 1] == b'_')
        {
            i += 1;
            continue;
        }
        let mut scheme_end = i + 1;
        while bytes.get(scheme_end).is_some_and(|&b| scheme_char(b)) {
            scheme_end += 1;
        }
        if bytes.get(scheme_end..scheme_end + 3) != Some(b"://") {
            i = scheme_end;
            continue;
        }
        let mut end = scheme_end + 3;
        while bytes.get(end).is_some_and(|&b| !envelope_end(b)) {
            end += 1;
        }
        blocked.push((run_start + i, run_start + end));
        if end - i > MAX_RAW_TOKEN_BYTES {
            i = end;
            continue;
        }
        let mut accepted_end = end;
        if url(&run[i..end]).is_none() {
            accepted_end = i + trim_url_suffix(&run[i..end], true);
        }
        push(
            input,
            run_start + i,
            run_start + accepted_end,
            SymbolKind::Url,
            candidates,
        );
        i = end;
    }
    // Explicit file paths use the whole text envelope, not the narrower
    // host/number alphabet. Otherwise `/tmp/a(b).txt` becomes a false prefix.
    // Reserve oversized files too, so their interiors return to ordinary
    // analysis instead of being indexed as shorter complete structures.
    let mut file_blocks = Vec::new();
    let mut i = 0;
    while i < bytes.len() {
        if envelope_end(bytes[i]) {
            i += 1;
            continue;
        }
        let start = i;
        while bytes.get(i).is_some_and(|&b| !envelope_end(b)) {
            i += 1;
        }
        let end = i;
        if overlaps(&blocked, run_start + start, run_start + end) {
            continue;
        }
        let raw = &run[start..end];
        if file(raw) && (explicit_file(raw) || lexical_kind(raw) == Some(SymbolKind::File)) {
            push(
                input,
                run_start + start,
                run_start + end,
                SymbolKind::File,
                candidates,
            );
            file_blocks.push((run_start + start, run_start + end));
        }
    }
    blocked.extend(file_blocks);
    blocked.sort_unstable_by_key(|&(start, _)| start);
    for link in finder.links(run) {
        let start = run_start + link.start();
        let end = run_start + link.end();
        if !overlaps(&blocked, start, end) {
            push(input, start, end, SymbolKind::Email, candidates);
        }
    }
    let mut i = 0;
    while i < bytes.len() {
        if !lexical_char(bytes[i]) {
            i += 1;
            continue;
        }
        let start = i;
        while bytes.get(i).is_some_and(|&b| lexical_char(b)) {
            i += 1;
        }
        let end = i;
        if overlaps(&blocked, run_start + start, run_start + end) {
            continue;
        }
        let raw = &run[start..end];
        if let Some(kind) = lexical_kind(raw) {
            push(input, run_start + start, run_start + end, kind, candidates);
        } else {
            let trimmed = raw.trim_end_matches('.');
            if let Some(kind) = lexical_kind(trimmed) {
                push(
                    input,
                    run_start + start,
                    run_start + start + trimmed.len(),
                    kind,
                    candidates,
                );
            }
        }
    }
}

fn push(input: &str, start: usize, end: usize, kind: SymbolKind, output: &mut Vec<Candidate>) {
    if start < end
        && end - start <= MAX_RAW_TOKEN_BYTES
        && scalar_envelope(input, start, end)
        && parse(kind, &input[start..end]).is_some()
    {
        output.push(Candidate { start, end, kind });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn observed(input: &str) -> Vec<(&str, SymbolKind)> {
        scan(input)
            .into_iter()
            .map(|candidate| (&input[candidate.start..candidate.end], candidate.kind))
            .collect()
    }

    #[test]
    fn literal_scope_and_outer_spans() {
        assert_eq!(
            observed("前文 https://Example.com/Running?q=100ms 后文"),
            [("https://Example.com/Running?q=100ms", SymbolKind::Url)]
        );
        assert_eq!(
            observed("<a href=\"https://Example.com/A#Part\">body 100ms</a> &amp;"),
            [
                ("https://Example.com/A#Part", SymbolKind::Url),
                ("100ms", SymbolKind::Numword),
                ("/a", SymbolKind::File)
            ]
        );
        assert_eq!(
            observed("/src/foo_bar.rs root@localhost"),
            [
                ("/src/foo_bar.rs", SymbolKind::File),
                ("root@localhost", SymbolKind::Email),
            ]
        );
        assert_eq!(
            observed("中https://x.test/A中"),
            [("https://x.test/A", SymbolKind::Url)]
        );
        assert!(
            observed("https://x.test/A\u{301} https://x.test/a|b https://x.test/%ZZ").is_empty()
        );
    }

    #[test]
    fn url_is_borrowed_and_keeps_literal_suffixes() {
        for raw in [
            "HTTP://Example.COM:00080/A/../%7e?q=a%26b#Part",
            "https://x.test?",
            "https://x.test#",
            "https://[::1]:8080/A",
            "file:///tmp/A",
        ] {
            let Some(Parsed::Url { uri, recall }) = parse(SymbolKind::Url, raw) else {
                panic!("valid literal URL rejected: {raw}");
            };
            assert_eq!(uri.as_str(), raw);
            assert!(recall.is_none());
            assert_eq!(uri.as_str().as_ptr(), raw.as_ptr());
        }
        let Some(Parsed::Url { uri, recall }) = parse(SymbolKind::Url, "https://x.test/a(foo)).")
        else {
            panic!()
        };
        assert_eq!(uri.as_str(), "https://x.test/a(foo)).");
        assert_eq!(recall.map(UriStr::as_str), Some("https://x.test/a(foo)"));
        let Some(Parsed::Url { recall, .. }) = parse(SymbolKind::Url, "https://x.test/?x=&amp;")
        else {
            panic!()
        };
        assert!(recall.is_none());
        assert_eq!(
            observed("https://x.test/A]}"),
            [("https://x.test/A", SymbolKind::Url)]
        );
        for raw in [
            "mailto:a@example.com",
            "urn:example:abc",
            "https://",
            "https://x.test/%ZZ",
            "https://x.test/a|b",
        ] {
            assert!(parse(SymbolKind::Url, raw).is_none(), "{raw}");
        }
    }

    #[test]
    fn finite_kind_priority_is_not_language_detection() {
        assert_eq!(
            observed(
                "v1.2.3 1.2.3 1.2.3.4 -1.25e-3 100ms snake_case kebab-case camelCase PascalCase C++"
            ),
            [
                ("v1.2.3", SymbolKind::Numword),
                ("1.2.3", SymbolKind::Numword),
                ("1.2.3.4", SymbolKind::Host),
                ("-1.25e-3", SymbolKind::Numword),
                ("100ms", SymbolKind::Numword),
                ("snake_case", SymbolKind::Numword),
                ("kebab-case", SymbolKind::Numword),
            ]
        );
        assert_eq!(
            observed(
                "README.md foo.rs example.com example.rs foo.weirdextension my.7file .gitignore"
            ),
            [
                ("README.md", SymbolKind::File),
                ("foo.rs", SymbolKind::File),
                ("example.com", SymbolKind::Host),
                ("example.rs", SymbolKind::File),
                ("foo.weirdextension", SymbolKind::Host),
                ("my.7file", SymbolKind::File),
                (".gitignore", SymbolKind::File),
            ]
        );
        for (raw, body) in [
            ("0x5fff", "5fff"),
            ("0XCAFE", "CAFE"),
            ("5fff", "5fff"),
            ("f5ff", "f5ff"),
            ("abc123", "abc123"),
        ] {
            assert!(
                matches!(parse(SymbolKind::Numword, raw), Some(Parsed::Numword(Numword::Hex(actual))) if actual==body)
            );
        }
        for raw in ["dead", "cafe", "face", "1234", "5ff", "100ms"] {
            assert!(!matches!(
                parse(SymbolKind::Numword, raw),
                Some(Parsed::Numword(Numword::Hex(_)))
            ));
        }
    }

    #[test]
    fn host_email_and_file_are_finite_ascii_rules() {
        let parsed = host("[2001:DB8::1]:00080", false).unwrap();
        assert_eq!(parsed.host, "[2001:DB8::1]");
        assert_eq!(parsed.port, Some("00080"));
        assert!(parsed.is_ip);
        assert!(host("localhost", true).is_some());
        assert!(host("localhost", false).is_none());
        for raw in [
            "-bad.example",
            "bad-.example",
            "a..example",
            "a.7",
            "example.com:",
            "example.com:abc",
            "[::g]",
            "::1",
        ] {
            assert!(host(raw, false).is_none(), "{raw}");
        }
        for raw in ["User+tag@Example.com", "root@localhost"] {
            assert!(parse(SymbolKind::Email, raw).is_some());
        }
        for raw in [
            ".bad@example.com",
            "bad..local@example.com",
            "a@example.com:80",
            "a@[::1]",
            "é@example.com",
        ] {
            assert!(parse(SymbolKind::Email, raw).is_none(), "{raw}");
        }
        for raw in [
            "/tmp/a",
            "./a",
            "../a",
            "~/a",
            r"C:\tmp\a",
            r"\\server\share\a",
            "src/foo.rs",
        ] {
            assert!(parse(SymbolKind::File, raw).is_some(), "{raw}");
        }
    }

    #[test]
    fn bounds_and_nonoverlap_are_independent_of_dictionary_options() {
        let short = format!("https://x.test/{}", "a".repeat(MAX_RAW_TOKEN_BYTES - 15));
        assert_eq!(short.len(), MAX_RAW_TOKEN_BYTES);
        assert_eq!(scan(&short).len(), 1);
        assert!(scan(&(short + "a")).is_empty());
        let input = "x /src/user@example.com/file.rs y https://x.test/100ms z";
        let spans = scan(input);
        assert_eq!(spans.len(), 2);
        assert!(spans.windows(2).all(|pair| pair[0].end <= pair[1].start));
        assert_eq!(spans[0].kind, SymbolKind::File);
        assert_eq!(spans[1].kind, SymbolKind::Url);
    }

    #[test]
    fn explicit_file_keeps_all_text_envelope_punctuation() {
        let raw = "/tmp/a(b)[2]{x}|name=foo,bar!.txt";
        assert!(parse(SymbolKind::File, raw).is_some());
        assert_eq!(observed(raw), [(raw, SymbolKind::File)]);
        let raw = r"C:\tmp\a(b)[2].txt";
        assert_eq!(observed(raw), [(raw, SymbolKind::File)]);
        let raw = "src/user@example.com/a(b).rs";
        assert_eq!(observed(raw), [(raw, SymbolKind::File)]);
        let raw = format!("/tmp/a({})file.rs", "x".repeat(MAX_RAW_TOKEN_BYTES));
        assert!(scan(&raw).is_empty());
        assert!(parse(SymbolKind::File, "/tmp/a\0b").is_none());
    }

    #[test]
    fn url_root_dot_preserves_full_and_optional_recall() {
        let raw = "https://Example.com.";
        let Some(Parsed::Url { uri, recall }) = parse(SymbolKind::Url, raw) else {
            panic!("DNS root dot is valid in a URL authority");
        };
        assert_eq!(uri.as_str(), raw);
        assert_eq!(recall.map(UriStr::as_str), Some("https://Example.com"));
        assert_eq!(observed(raw), [(raw, SymbolKind::Url)]);
        let raw = "https://Example.com.:00080/A";
        let Some(Parsed::Url { uri, recall }) = parse(SymbolKind::Url, raw) else {
            panic!("DNS root dot before a decimal port is valid");
        };
        assert_eq!(uri.as_str(), raw);
        assert!(recall.is_none());
        for raw in [
            "https://example.com..",
            "https://%65xample.com/A",
            "https://example_com/A",
        ] {
            assert!(
                parse(SymbolKind::Url, raw).is_some(),
                "URI reg-name is not a DNS assertion: {raw}"
            );
        }
        let invalid = "https://example.com.:abc";
        assert!(parse(SymbolKind::Url, invalid).is_none(), "{invalid}");
    }

    #[test]
    fn decimal_literals_need_digits_on_either_side_not_both() {
        for raw in [".5", "5.", "-.5", "-0.5", "+.5", "5.e2", ".5e-2"] {
            assert!(
                matches!(
                    parse(SymbolKind::Numword, raw),
                    Some(Parsed::Numword(Numword::Literal))
                ),
                "{raw}"
            );
            assert_eq!(observed(raw), [(raw, SymbolKind::Numword)]);
        }
        for raw in [".", "+.", "-.", ".e2", "5.e"] {
            assert!(
                !matches!(
                    parse(SymbolKind::Numword, raw),
                    Some(Parsed::Numword(Numword::Literal))
                ),
                "{raw}"
            );
        }
    }
}
