//! Opt-in stage measurements. Timing never substitutes for correctness assertions.
use crate::analysis::{self, Options};
use crate::routes::Route;
use charabia::{TokenKind, TokenizerBuilder};
use qdrant_rust_stemmers::Stemmer;
use std::{hint::black_box, time::Instant};

const ITERATIONS: usize = 100;
const REPEATS: usize = 5;

#[test]
#[ignore = "run by the benchmark suite, outside correctness tests"]
fn benchmark_direct_pipeline() {
    let texts = [
        (
            "english",
            "The children were running through the garden while the teachers were reading books and sharing their happy memories together.",
        ),
        (
            "korean",
            "한국어의형태해석을실시할수있습니다 한국어 검색 데이터베이스",
        ),
        (
            "mixed",
            "PostgreSQL 数据库 全文检索 café 日本語の検索 한국어 검색 /usr/local v18.6",
        ),
        (
            "accented",
            "Les enfants ont écrit des lettres aimées. IŞIKLAR kitapları hästarna röster gården.",
        ),
    ];
    let mut builder = TokenizerBuilder::default();
    let tokenizer = builder.build();
    let start = Instant::now();
    black_box(tokenizer.segment(texts[0].1).count());
    let first_raw_segment_ns = start.elapsed().as_nanos();
    let start = Instant::now();
    black_box(analysis::segment_counted(texts[0].1).unwrap());
    let first_segment_with_language_ns = start.elapsed().as_nanos();

    // These are request-local benchmark states constructed by the same builder
    // used at INIT. They are not a backend-global cache or a PG live-state count.
    let mut fst_initializations = Vec::new();
    let sets = Route::ALL
        .into_iter()
        .filter(|route| !route.is_display())
        .map(|route| {
            let start = Instant::now();
            let state = crate::stopwords::build(route, true, &[], &[]).unwrap();
            let fst_init_ns = start.elapsed().as_nanos();
            let start = Instant::now();
            let dictionary = Options::new(route, true, true, &[], &[]).unwrap();
            let options_init_ns = start.elapsed().as_nanos();
            assert_eq!(dictionary.fst_bytes(), state.fst.as_fst().as_bytes().len());
            fst_initializations.push(serde_json::json!({
            "route":route.alias(),"fst_build_ns":fst_init_ns,"full_options_init_ns":options_init_ns,
            "fst_bytes":state.fst.as_fst().as_bytes().len(),"keys":state.fst.len(),
            "excluded_builtin":state.excluded_builtin_count}));
            state
        })
        .collect::<Vec<_>>();
    let start = Instant::now();
    let options =
        [(false, false), (false, true), (true, false), (true, true)].map(|(stem, stop)| {
            Route::ALL
                .into_iter()
                .filter(|route| !route.is_display())
                .map(|route| Options::new(route, stem, stop, &[], &[]).unwrap())
                .collect::<Vec<_>>()
        });
    let four_configurations_init_ns = start.elapsed().as_nanos();
    let stemmers = Route::ALL
        .into_iter()
        .filter(|route| !route.is_display())
        .map(|route| route.algorithm().map(Stemmer::create))
        .collect::<Vec<_>>();
    let mut inputs = Vec::new();
    for (name, input) in texts {
        let (spans, detector_calls) = analysis::segment_counted(input).unwrap();
        assert!(!spans.iter().any(|span| span.route.is_display()));
        let prepared = spans
            .iter()
            .filter_map(|span| {
                let prepared = analysis::prepare(&input[span.start..span.end], span.route).unwrap();
                (prepared.kind == TokenKind::Word && !prepared.key.is_empty())
                    .then_some((span.route, prepared.key))
            })
            .collect::<Vec<_>>();
        let mut samples = Vec::new();
        for mode in [
            "raw_segment",
            "segment_with_language",
            "prepare",
            "fst_lookup_prepared",
            "snowball_prepared",
            "surface_no_stop",
            "surface_stop",
            "dual_no_stop",
            "dual_stop",
            "whole_pipeline",
        ] {
            let mut elapsed = Vec::new();
            let mut work = Vec::new();
            let mut supplemental_calls = Vec::new();
            for _ in 0..REPEATS {
                let mut count = 0usize;
                let mut calls = 0usize;
                let start = Instant::now();
                for _ in 0..ITERATIONS {
                    match mode {
                        "raw_segment" => {
                            count += black_box(tokenizer.segment(black_box(input))).count()
                        }
                        "segment_with_language" => {
                            let (current, actual_calls) =
                                analysis::segment_counted(black_box(input)).unwrap();
                            calls += actual_calls;
                            count += black_box(current).len();
                        }
                        "prepare" => {
                            for span in &spans {
                                black_box(
                                    analysis::prepare(
                                        black_box(&input[span.start..span.end]),
                                        span.route,
                                    )
                                    .unwrap(),
                                );
                                count += 1;
                            }
                        }
                        "fst_lookup_prepared" => {
                            for (route, key) in &prepared {
                                black_box(
                                    sets[route.id() as usize - 1].fst.contains(black_box(key)),
                                );
                                count += 1;
                            }
                        }
                        "snowball_prepared" => {
                            for (route, key) in &prepared {
                                if key.chars().all(|c| {
                                    c.is_alphabetic()
                                        || unicode_normalization::char::is_combining_mark(c)
                                }) && let Some(stemmer) = &stemmers[route.id() as usize - 1]
                                {
                                    black_box(stemmer.stem(black_box(key)));
                                    count += 1;
                                }
                            }
                        }
                        _ => {
                            let current;
                            let source = if mode == "whole_pipeline" {
                                let (fresh, actual_calls) =
                                    analysis::segment_counted(black_box(input)).unwrap();
                                calls += actual_calls;
                                current = fresh;
                                &current
                            } else {
                                &spans
                            };
                            let flag_index = match mode {
                                "surface_no_stop" => 0,
                                "surface_stop" => 1,
                                "dual_no_stop" => 2,
                                _ => 3,
                            };
                            for span in source {
                                black_box(
                                    analysis::normalize(
                                        black_box(&input[span.start..span.end]),
                                        span.route,
                                        &options[flag_index][span.route.id() as usize - 1],
                                    )
                                    .unwrap(),
                                );
                                count += 1;
                            }
                        }
                    }
                }
                elapsed.push(start.elapsed().as_nanos());
                work.push(count);
                supplemental_calls.push(calls);
                if matches!(mode, "segment_with_language" | "whole_pipeline") {
                    assert_eq!(
                        calls,
                        detector_calls * ITERATIONS,
                        "detector calls are per original script group"
                    );
                }
            }
            samples.push(
                serde_json::json!({"stage":mode,"iterations":ITERATIONS,"elapsed_ns":elapsed,
                "work_items":work,"actual_supplement_detector_calls":supplemental_calls}),
            );
        }
        inputs.push(serde_json::json!({"name":name,"text":input,"input_bytes":input.len(),
            "spans":spans.len(),"word_keys":prepared.len(),"detector_calls_per_input":detector_calls,"samples":samples}));
    }
    println!(
        "PGCB_BENCHMARK={}",
        serde_json::json!({
        "process_id":std::process::id(),"first_raw_segment_ns":first_raw_segment_ns,
        "first_segment_with_language_ns":first_segment_with_language_ns,
        "first_sample_order":"raw segment first, then completion; not two independent cold starts",
        "per_route_effective_fst_init":fst_initializations,"four_configurations_init_ns":four_configurations_init_ns,
        "state_scope":"owned local Rust states; not PostgreSQL dictCtx live counts",
        "inputs":inputs})
    );
}
