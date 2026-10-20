use sha2::{Digest, Sha256};
use std::{env, fs, path::Path};

fn main() {
    for (path, key) in [
        ("Cargo.lock", "PG_CHARABIA_LOCK_SHA256"),
        ("resources/manifest.json", "PG_CHARABIA_RESOURCES_SHA256"),
    ] {
        println!("cargo:rerun-if-changed={path}");
        let bytes = fs::read(Path::new(&env::var("CARGO_MANIFEST_DIR").unwrap()).join(path))
            .expect("required build manifest is missing");
        println!("cargo:rustc-env={key}={:x}", Sha256::digest(bytes));
    }
    println!(
        "cargo:rustc-env=PG_CHARABIA_TARGET={}",
        env::var("TARGET").unwrap()
    );
}
