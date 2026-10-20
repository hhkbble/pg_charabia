//! Explicit opt-in benchmark; the normal unit suite never measures performance.
use crate::analysis::{self, Options};
use charabia::TokenizerBuilder;
use std::{hint::black_box, time::Instant};

#[test]
#[ignore = "run by the benchmark suite, outside correctness tests"]
fn benchmark_direct_pipeline() {
    let input = "PostgreSQL 数据库 全文检索 café 日本語の検索 한국어 검색 /usr/local v18.6";
    let options = Options::default();
    let mut builder = TokenizerBuilder::default();
    builder.lossy_normalization(true).create_char_map(false);
    let tokenizer = builder.build();
    let cold = Instant::now();
    black_box(tokenizer.tokenize(input).count());
    let cold_ns = cold.elapsed().as_nanos();
    let iterations = 1000;
    let mut samples = Vec::new();
    for mode in ["segment", "tokenize", "route_pipeline"] {
        let start = Instant::now();
        let mut count = 0;
        for _ in 0..iterations {
            match mode {
                "segment" => count += black_box(tokenizer.segment(black_box(input))).count(),
                "tokenize" => count += black_box(tokenizer.tokenize(black_box(input))).count(),
                _ => {
                    for span in analysis::segment(black_box(input)).unwrap() {
                        black_box(
                            analysis::normalize(&input[span.start..span.end], span.route, &options)
                                .unwrap(),
                        );
                        count += 1;
                    }
                }
            }
        }
        let ns = start.elapsed().as_nanos();
        samples.push(serde_json::json!({"mode":mode,"iterations":iterations,
            "input_bytes":input.len(),"tokens":count,"elapsed_ns":ns,
            "bytes_per_second":(input.len()*iterations) as f64 / (ns as f64 / 1e9)}));
    }
    println!(
        "PGCB_BENCHMARK={}",
        serde_json::json!({"cold_tokenize_ns":cold_ns,"samples":samples})
    );
}
