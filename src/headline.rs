//! Rendering policy around PostgreSQL's headline selector. Matching remains in PG.

use std::borrow::Cow;

pub const MAX_MARKER_BYTES: usize = 256;
pub const MAX_ENTRY_BYTES: usize = u16::MAX as usize;
pub const MAX_OUTPUT_BYTES: usize = 8 * 1024 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ErrorKind {
    InvalidInput,
    Limit,
    Unsupported,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Error {
    pub kind: ErrorKind,
    pub message: String,
}

impl Error {
    pub fn new(kind: ErrorKind, message: impl Into<String>) -> Self {
        Self {
            kind,
            message: message.into(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Options {
    pub start_sel: String,
    pub stop_sel: String,
    pub fragment_delimiter: String,
    pub min_words: u32,
    pub max_words: u32,
    pub max_fragments: u32,
    pub highlight_all: bool,
    pub escape: bool,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            start_sel: "<b>".into(),
            stop_sel: "</b>".into(),
            fragment_delimiter: " ... ".into(),
            min_words: 15,
            max_words: 35,
            max_fragments: 0,
            highlight_all: false,
            escape: true,
        }
    }
}

impl Options {
    /// Values have already been unquoted by PG's deserialize_deflist.
    pub fn parse(values: impl IntoIterator<Item = (String, String)>) -> Result<Self, Error> {
        let mut result = Self::default();
        let mut seen = [false; 9];
        for (name, value) in values {
            let name = name.to_ascii_lowercase();
            let index = match name.as_str() {
                "startsel" => 0,
                "stopsel" => 1,
                "fragmentdelimiter" => 2,
                "minwords" => 3,
                "maxwords" => 4,
                "maxfragments" => 5,
                "highlightall" => 6,
                "shortword" => 7,
                "escape" => 8,
                _ => {
                    return Err(Error::new(
                        ErrorKind::InvalidInput,
                        format!("unknown headline option: {name}"),
                    ));
                }
            };
            if std::mem::replace(&mut seen[index], true) {
                return Err(Error::new(
                    ErrorKind::InvalidInput,
                    format!("duplicate headline option: {name}"),
                ));
            }
            match index {
                0..=2 => {
                    if value.len() > MAX_MARKER_BYTES {
                        return Err(Error::new(
                            ErrorKind::Limit,
                            format!("headline {name} exceeds 256 bytes"),
                        ));
                    }
                    if value.contains('\0') {
                        return Err(Error::new(
                            ErrorKind::InvalidInput,
                            "NUL in headline marker",
                        ));
                    }
                    match index {
                        0 => result.start_sel = value,
                        1 => result.stop_sel = value,
                        _ => result.fragment_delimiter = value,
                    }
                }
                3 => result.min_words = integer(&name, &value)?,
                4 => result.max_words = integer(&name, &value)?,
                5 => result.max_fragments = integer(&name, &value)?,
                6 => result.highlight_all = boolean(&name, &value)?,
                7 => {
                    if integer(&name, &value)? != 0 {
                        return Err(Error::new(
                            ErrorKind::Unsupported,
                            "headline ShortWord supports only 0",
                        ));
                    }
                }
                8 => result.escape = boolean(&name, &value)?,
                _ => unreachable!(),
            }
        }
        if result.min_words == 0
            || result.min_words >= result.max_words
            || result.max_words > 10_000
        {
            return Err(Error::new(
                ErrorKind::InvalidInput,
                "headline requires 1 <= MinWords < MaxWords <= 10000",
            ));
        }
        if result.max_fragments > 100 {
            return Err(Error::new(
                ErrorKind::InvalidInput,
                "headline MaxFragments must be between 0 and 100",
            ));
        }
        if result.highlight_all && (seen[3] || seen[4] || result.max_fragments > 0) {
            return Err(Error::new(
                ErrorKind::InvalidInput,
                "HighlightAll conflicts with explicit MinWords/MaxWords or positive MaxFragments",
            ));
        }
        Ok(result)
    }

    /// Fully specified values prevent PG's permissive bool/default policies leaking in.
    pub fn selector_options(&self) -> [(&'static str, String); 8] {
        [
            ("StartSel", self.start_sel.clone()),
            ("StopSel", self.stop_sel.clone()),
            ("FragmentDelimiter", self.fragment_delimiter.clone()),
            ("MinWords", self.min_words.to_string()),
            ("MaxWords", self.max_words.to_string()),
            ("MaxFragments", self.max_fragments.to_string()),
            ("HighlightAll", self.highlight_all.to_string()),
            ("ShortWord", "0".into()),
        ]
    }
}

fn integer(name: &str, value: &str) -> Result<u32, Error> {
    let value = value.trim();
    let digits = value.strip_prefix('+').unwrap_or(value);
    if digits.is_empty() || !digits.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err(Error::new(
            ErrorKind::InvalidInput,
            format!("invalid headline integer for {name}"),
        ));
    }
    digits.parse().map_err(|_| {
        Error::new(
            ErrorKind::InvalidInput,
            format!("headline integer out of range for {name}"),
        )
    })
}

fn boolean(name: &str, value: &str) -> Result<bool, Error> {
    match value.to_ascii_lowercase().as_str() {
        "1" | "on" | "true" | "t" | "y" | "yes" => Ok(true),
        "0" | "off" | "false" | "f" | "n" | "no" => Ok(false),
        _ => Err(Error::new(
            ErrorKind::InvalidInput,
            format!("invalid headline boolean for {name}"),
        )),
    }
}

/// Escape only source text. The three markers are intentionally trusted markup.
pub fn escape_html(raw: &str, escape: bool) -> Result<Cow<'_, str>, Error> {
    let length = rendered_len(raw, escape)?;
    if !escape || length == raw.len() {
        return Ok(Cow::Borrowed(raw));
    }
    let mut output = String::with_capacity(length);
    for character in raw.chars() {
        match character {
            '&' => output.push_str("&amp;"),
            '<' => output.push_str("&lt;"),
            '>' => output.push_str("&gt;"),
            '"' => output.push_str("&quot;"),
            '\'' => output.push_str("&#39;"),
            _ => output.push(character),
        }
    }
    Ok(Cow::Owned(output))
}

pub fn rendered_len(raw: &str, escape: bool) -> Result<usize, Error> {
    let length = if escape {
        raw.bytes().try_fold(0usize, |total, byte| {
            total.checked_add(match byte {
                b'&' => 5,
                b'<' | b'>' => 4,
                b'"' => 6,
                b'\'' => 5,
                _ => 1,
            })
        })
    } else {
        Some(raw.len())
    };
    length
        .filter(|&length| length <= MAX_ENTRY_BYTES)
        .ok_or_else(|| {
            Error::new(
                ErrorKind::Limit,
                "headline entry exceeds 65535 bytes after escaping",
            )
        })
}

#[derive(Clone, Copy, Debug)]
pub struct OutputEntry {
    pub bytes: usize,
    pub in_fragment: bool,
    pub repeated: bool,
    pub selected: bool,
    pub replace: bool,
    pub skip: bool,
}

/// Mirrors only generateHeadline's byte accounting, not its selection algorithm.
pub fn output_size(
    entries: impl IntoIterator<Item = OutputEntry>,
    options: &Options,
) -> Result<usize, Error> {
    let mut output = 0usize;
    let mut in_fragment = false;
    let mut had_fragment = false;
    for entry in entries {
        if entry.repeated {
            continue;
        }
        if !entry.in_fragment {
            in_fragment = false;
            continue;
        }
        if !in_fragment {
            if had_fragment {
                add_output(&mut output, options.fragment_delimiter.len())?;
            }
            in_fragment = true;
            had_fragment = true;
        }
        if entry.replace {
            add_output(&mut output, 1)?;
        } else if !entry.skip {
            add_output(&mut output, entry.bytes)?;
            if entry.selected {
                add_output(&mut output, options.start_sel.len())?;
                add_output(&mut output, options.stop_sel.len())?;
            }
        }
    }
    Ok(output)
}

fn add_output(output: &mut usize, bytes: usize) -> Result<(), Error> {
    *output = output
        .checked_add(bytes)
        .filter(|&length| length <= MAX_OUTPUT_BYTES)
        .ok_or_else(|| Error::new(ErrorKind::Limit, "headline output exceeds 8388608 bytes"))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn options(values: &[(&str, &str)]) -> Result<Options, Error> {
        Options::parse(
            values
                .iter()
                .map(|(key, value)| ((*key).into(), (*value).into())),
        )
    }

    #[test]
    fn options_are_strict_and_case_insensitive() {
        assert!(options(&[("Escape", "YES"), ("escape", "false")]).is_err());
        assert!(options(&[("unknown", "true")]).is_err());
        assert!(options(&[("HighlightAll", "garbage")]).is_err());
        assert!(options(&[("MaxWords", "2x")]).is_err());
        assert!(options(&[("MaxWords", "-1")]).is_err());
        assert!(!options(&[("eScApE", "off")]).unwrap().escape);
    }

    #[test]
    fn selector_limits_and_highlight_conflicts() {
        assert_eq!(
            options(&[("ShortWord", "3")]).unwrap_err().kind,
            ErrorKind::Unsupported
        );
        assert!(options(&[("HighlightAll", "true"), ("MinWords", "15")]).is_err());
        assert!(options(&[("HighlightAll", "true"), ("MaxFragments", "1")]).is_err());
        assert!(options(&[("MaxFragments", "101")]).is_err());
        assert!(options(&[("MinWords", "0")]).is_err());
        assert!(options(&[("MaxWords", "10001")]).is_err());
        assert!(options(&[("MaxWords", "15")]).is_err());
        assert!(options(&[("HighlightAll", "true"), ("MaxFragments", "0")]).is_ok());
        assert!(options(&[("StartSel", ""), ("StopSel", "")]).is_ok());
        assert!(options(&[("StartSel", &"界".repeat(86))]).is_err());
    }

    #[test]
    fn source_escaping_preserves_unicode_and_only_five_ascii_characters() {
        assert_eq!(
            escape_html("中文 &<>\"' Café cafe\u{301}", true).unwrap(),
            "中文 &amp;&lt;&gt;&quot;&#39; Café cafe\u{301}"
        );
        assert!(matches!(
            escape_html("Café", true).unwrap(),
            Cow::Borrowed(_)
        ));
        assert_eq!(escape_html("<b>&", false).unwrap(), "<b>&");
        assert_eq!(rendered_len(&"&".repeat(13_107), true).unwrap(), 65_535);
        assert_eq!(
            rendered_len(&"&".repeat(13_108), true).unwrap_err().kind,
            ErrorKind::Limit
        );
    }

    #[test]
    fn output_accounting_handles_repeated_and_fragment_boundaries() {
        let base = OutputEntry {
            bytes: 3,
            in_fragment: true,
            repeated: false,
            selected: true,
            replace: false,
            skip: false,
        };
        let entries = [
            base,
            OutputEntry {
                repeated: true,
                in_fragment: false,
                bytes: usize::MAX,
                ..base
            },
            base,
            OutputEntry {
                in_fragment: false,
                ..base
            },
            base,
        ];
        assert_eq!(
            output_size(entries, &Options::default()).unwrap(),
            3 * (3 + 7) + 5
        );
        let big = OutputEntry {
            bytes: MAX_OUTPUT_BYTES,
            selected: false,
            ..base
        };
        assert_eq!(
            output_size([big], &Options::default()).unwrap(),
            MAX_OUTPUT_BYTES
        );
        assert_eq!(
            output_size([big, base], &Options::default())
                .unwrap_err()
                .kind,
            ErrorKind::Limit
        );
        assert_eq!(
            output_size(
                [OutputEntry {
                    replace: true,
                    ..big
                }],
                &Options::default()
            )
            .unwrap(),
            1
        );
    }
}
