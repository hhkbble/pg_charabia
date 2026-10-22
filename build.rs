use sha2::{Digest, Sha256};
use std::{collections::BTreeSet, env, fs, path::PathBuf};

#[allow(dead_code)]
#[path = "src/language_data.rs"]
mod language_data;
#[allow(dead_code)]
#[path = "src/prepare.rs"]
mod prepare;

fn main() {
    let root = PathBuf::from(env::var("CARGO_MANIFEST_DIR").unwrap());
    let out = PathBuf::from(env::var("OUT_DIR").unwrap());
    for (path, key) in [
        ("Cargo.lock", "PG_CHARABIA_LOCK_SHA256"),
        ("resources/manifest.json", "PG_CHARABIA_RESOURCES_SHA256"),
    ] {
        println!("cargo:rerun-if-changed={path}");
        let bytes = fs::read(root.join(path)).expect("required build manifest is missing");
        println!("cargo:rustc-env={key}={:x}", Sha256::digest(bytes));
    }
    for path in [
        "src/prepare.rs",
        "src/language_data.rs",
        "resources/english/words.txt",
    ] {
        println!("cargo:rerun-if-changed={path}");
    }
    let manifest: serde_json::Value =
        serde_json::from_slice(&fs::read(root.join("resources/manifest.json")).unwrap()).unwrap();
    let mut generated = String::from("pub(crate) const STOP_DATA: [Option<StopData>; 16] = [\n");
    for route in language_data::LanguageId::ALL {
        let Some(code) = route.stopword_key() else {
            generated.push_str("None,\n");
            continue;
        };
        let words = stop_words::lookup(code).expect("fixed language stopword source");
        let mut keys = BTreeSet::new();
        let mut excluded = 0;
        for raw in words {
            let p = prepare::prepare(raw, route).expect("valid fixed stopword input");
            if p.kind == charabia::TokenKind::Word && !p.key.is_empty() {
                keys.insert(p.key);
            } else {
                excluded += 1;
            }
        }
        let mut hash = Sha256::new();
        for key in &keys {
            hash.update((key.len() as u64).to_le_bytes());
            hash.update(key.as_bytes());
        }
        let canonical_hash = format!("{:x}", hash.finalize());
        let fst = fst::Set::from_iter(&keys).expect("sorted stopword keys");
        let bytes = fst.as_fst().as_bytes();
        fs::write(out.join(format!("{}.fst", route.alias())), bytes).unwrap();
        let recorded = &manifest["canonical_stop_sets"][route.alias()];
        let compiled = &manifest["compiled_stop_sets"][route.alias()];
        assert_eq!(
            canonical_hash,
            recorded["sha256"].as_str().unwrap(),
            "canonical stop keys changed"
        );
        assert_eq!(keys.len(), recorded["entries"].as_u64().unwrap() as usize);
        assert_eq!(
            excluded,
            recorded["excluded_builtin_entries"].as_u64().unwrap() as usize
        );
        assert_eq!(
            format!("{:x}", Sha256::digest(bytes)),
            compiled["fst_sha256"].as_str().unwrap(),
            "stop FST bytes changed"
        );
        assert_eq!(
            words.len(),
            compiled["raw_entries"].as_u64().unwrap() as usize
        );
        assert_eq!(
            words.len() - keys.len() - excluded,
            compiled["duplicates"].as_u64().unwrap() as usize
        );
        generated.push_str(&format!(r#"Some(StopData {{ bytes: include_bytes!(concat!(env!("OUT_DIR"), "/{}.fst")), excluded: {} }}),
"#, route.alias(), excluded));
    }
    generated.push_str("];\n");
    let input = fs::read_to_string(root.join("resources/english/words.txt"))
        .expect("the committed English TXT is required; builds never download it");
    assert_eq!(
        format!("{:x}", Sha256::digest(input.as_bytes())),
        manifest["english_lexicon"]["txt_sha256"].as_str().unwrap(),
        "English TXT identity differs from manifest"
    );
    let mut previous = None;
    for key in input.lines() {
        assert!(
            !key.is_empty() && key.bytes().all(|c| c.is_ascii_lowercase()),
            "English keys must be lowercase ASCII letters"
        );
        assert!(
            previous.is_none_or(|p: &str| p < key),
            "English keys must be sorted and unique"
        );
        previous = Some(key);
    }
    assert!(input.ends_with('\n'), "English TXT must end with LF");
    let english = fst::Set::from_iter(input.lines()).expect("sorted English input");
    assert_eq!(
        format!("{:x}", Sha256::digest(english.as_fst().as_bytes())),
        manifest["english_lexicon"]["fst_sha256"].as_str().unwrap(),
        "English FST identity differs from manifest"
    );
    assert_eq!(
        english.len(),
        manifest["english_lexicon"]["entries"].as_u64().unwrap() as usize
    );
    fs::write(out.join("english.fst"), english.as_fst().as_bytes()).unwrap();
    generated.push_str("pub(crate) const ENGLISH_BYTES: &[u8] = include_bytes!(concat!(env!(\"OUT_DIR\"), \"/english.fst\"));\n");
    fs::write(out.join("word_sets.rs"), generated).unwrap();
    println!(
        "cargo:rustc-env=PG_CHARABIA_TARGET={}",
        env::var("TARGET").unwrap()
    );
}
