//! Catalog-backed configuration snapshots and atomic invoker-owned management.
use crate::{
    pg_ffi,
    routes::{ROUTE_COUNT, Route},
    settings::{DictionaryParameters, Settings},
};
use pgrx::{
    JsonB, Spi, pg_sys,
    spi::{quote_literal, quote_qualified_identifier},
};
use serde::Deserialize;

fn query_json(sql: &str) -> serde_json::Value {
    Spi::get_one::<JsonB>(sql)
        .unwrap_or_else(|e| pg_ffi::invalid(format!("catalog query failed: {e}")))
        .unwrap_or_else(|| pg_ffi::invalid("configuration not found"))
        .0
}
fn execute(sql: &str) {
    Spi::run(sql).unwrap_or_else(|e| pg_ffi::invalid(format!("configuration DDL failed: {e}")));
}
#[derive(Debug, Deserialize)]
struct CatalogDictionary {
    id: u32,
    namespace: String,
    name: String,
    template_ok: bool,
    options: String,
    token: i32,
    sequence: i32,
}
#[derive(Debug, Deserialize)]
struct CatalogConfig {
    id: u32,
    namespace: String,
    name: String,
    parser_ok: bool,
    dictionaries: Vec<CatalogDictionary>,
}
#[derive(Debug, Clone)]
pub struct DictionaryRef {
    pub id: pg_sys::Oid,
    pub qualified_name: String,
}
#[derive(Debug)]
pub struct Snapshot {
    pub id: pg_sys::Oid,
    pub namespace: String,
    pub name: String,
    pub dictionaries: Vec<DictionaryRef>,
    pub settings: Settings,
}
impl Snapshot {
    pub fn load(id: pg_sys::Oid) -> Self {
        Self::read(id, false)
    }
    fn read(id: pg_sys::Oid, latest: bool) -> Self {
        let sql = format!(
            r#"SELECT pg_catalog.jsonb_build_object(
            'id',c.oid::int8,'namespace',n.nspname,'name',c.cfgname,
            'parser_ok',c.cfgparser=(SELECT p.oid FROM pg_catalog.pg_ts_parser p JOIN pg_catalog.pg_namespace pn ON pn.oid=p.prsnamespace WHERE pn.nspname='charabia' AND p.prsname='parser'),
            'dictionaries',COALESCE((SELECT pg_catalog.jsonb_agg(pg_catalog.jsonb_build_object(
                'id',d.oid::int8,'namespace',dn.nspname,'name',d.dictname,'options',COALESCE(d.dictinitoption,''),'token',m.maptokentype,'sequence',m.mapseqno,
                'template_ok',d.dicttemplate=(SELECT t.oid FROM pg_catalog.pg_ts_template t JOIN pg_catalog.pg_namespace tn ON tn.oid=t.tmplnamespace WHERE tn.nspname='charabia' AND t.tmplname='template')) ORDER BY m.maptokentype,m.mapseqno)
                FROM pg_catalog.pg_ts_config_map m JOIN pg_catalog.pg_ts_dict d ON d.oid=m.mapdict JOIN pg_catalog.pg_namespace dn ON dn.oid=d.dictnamespace WHERE m.mapcfg=c.oid),'[]'::jsonb))
            FROM pg_catalog.pg_ts_config c JOIN pg_catalog.pg_namespace n ON n.oid=c.cfgnamespace WHERE c.oid={}"#,
            id.to_u32()
        );
        let value = if latest {
            pg_ffi::latest_catalog_json(&sql)
                .unwrap_or_else(|| pg_ffi::invalid("configuration not found"))
        } else {
            query_json(&sql)
        };
        let c: CatalogConfig = serde_json::from_value(value).unwrap_or_else(|e| {
            pg_ffi::invalid(format!("invalid configuration catalog snapshot: {e}"))
        });
        if !c.parser_ok || c.dictionaries.len() != ROUTE_COUNT {
            pg_ffi::invalid(
                "configuration must have the charabia parser and exactly 71 single dictionary mappings",
            );
        }
        let mut settings = Settings::default();
        let mut refs = Vec::with_capacity(ROUTE_COUNT);
        let mut fingerprint = None;
        for (i, d) in c.dictionaries.into_iter().enumerate() {
            if d.token != (i + 1) as i32 || d.sequence != 1 || !d.template_ok {
                pg_ffi::invalid("incomplete, duplicate or foreign dictionary mapping");
            }
            let parameters = pg_ffi::options_from_catalog(&d.options);
            if parameters.route.id() != d.token {
                pg_ffi::invalid("dictionary Route differs from its token mapping");
            }
            if let Some(ref fp) = fingerprint {
                if fp != &parameters.settings_fingerprint
                    || settings.stemming != parameters.stemming
                    || settings.builtin_stopwords != parameters.builtin_stopwords
                {
                    pg_ffi::invalid("inconsistent dictionary settings or fingerprint");
                }
            } else {
                fingerprint = Some(parameters.settings_fingerprint.clone());
                settings.stemming = parameters.stemming;
                settings.builtin_stopwords = parameters.builtin_stopwords;
            }
            let code = parameters.route.alias().to_owned();
            if !parameters.add.is_empty() {
                settings.stopwords_add.insert(code.clone(), parameters.add);
            }
            if !parameters.keep.is_empty() {
                settings.stopwords_keep.insert(code, parameters.keep);
            }
            refs.push(DictionaryRef {
                id: pg_sys::Oid::from(d.id),
                qualified_name: quote_qualified_identifier(&d.namespace, &d.name),
            });
        }
        pg_ffi::checked(settings.validate());
        if fingerprint.as_deref() != Some(settings.fingerprint().as_str()) {
            pg_ffi::invalid("configuration settings fingerprint does not match its dictionaries");
        }
        Self {
            id: pg_sys::Oid::from(c.id),
            namespace: c.namespace,
            name: c.name,
            dictionaries: refs,
            settings,
        }
    }
    pub fn qualified_name(&self) -> String {
        quote_qualified_identifier(&self.namespace, &self.name)
    }
    pub fn dictionary(&self, route: Route) -> pg_sys::Oid {
        if route.is_display() {
            pg_ffi::invalid("display-only tokens have no dictionary");
        }
        self.dictionaries[route.id() as usize - 1].id
    }
    fn for_update(id: pg_sys::Oid) -> Self {
        pg_ffi::lock_object(pg_sys::TSConfigRelationId, id);
        let before = Self::read(id, true);
        let mut ids: Vec<_> = before.dictionaries.iter().map(|d| d.id.to_u32()).collect();
        ids.sort_unstable();
        ids.dedup();
        // COPY does not lock its source configuration; its dependency records
        // hold dictionary AccessShareLocks. Lock all dictionaries before the
        // post-wait shared check so COPY cannot cross the validation/DDL gap.
        for id in ids {
            pg_ffi::lock_object(pg_sys::TSDictionaryRelationId, pg_sys::Oid::from(id));
        }
        let current = Self::read(id, true);
        if !before
            .dictionaries
            .iter()
            .map(|d| d.id)
            .eq(current.dictionaries.iter().map(|d| d.id))
        {
            pg_ffi::invalid(
                "configuration mappings changed while locking; retry management operation",
            );
        }
        current.require_mutable();
        current
    }
    fn require_mutable(&self) {
        pg_ffi::require_owner(pg_sys::TSConfigRelationId, self.id);
        if extension_member(pg_sys::TSConfigRelationId, self.id) {
            pg_ffi::invalid(
                "extension member configurations are fixed; create an independent instance",
            );
        }
        let ids = self
            .dictionaries
            .iter()
            .map(|d| d.id.to_u32().to_string())
            .collect::<Vec<_>>()
            .join(",");
        let shared=pg_ffi::latest_catalog_json(&format!("SELECT pg_catalog.to_jsonb(EXISTS(SELECT 1 FROM pg_catalog.pg_ts_config_map WHERE mapdict IN ({ids}) AND mapcfg<>{}))",self.id.to_u32())).and_then(|v|v.as_bool()).unwrap_or(true);
        if shared {
            pg_ffi::invalid(
                "configuration shares dictionaries through COPY; use charabia.create_config for an independent instance",
            );
        }
        for d in &self.dictionaries {
            pg_ffi::require_owner(pg_sys::TSDictionaryRelationId, d.id);
            if extension_member(pg_sys::TSDictionaryRelationId, d.id) {
                pg_ffi::invalid(
                    "extension member dictionaries cannot be changed; use charabia.create_config",
                );
            }
        }
    }
}
fn extension_member(class: pg_sys::Oid, id: pg_sys::Oid) -> bool {
    pg_ffi::latest_catalog_json(&format!("SELECT pg_catalog.to_jsonb(EXISTS(SELECT 1 FROM pg_catalog.pg_depend WHERE classid={} AND objid={} AND deptype='e'))",class.to_u32(),id.to_u32())).and_then(|v|v.as_bool()).unwrap_or(true)
}
fn dictionary_parameters(route: Route, settings: &Settings) -> String {
    format!(
        "Route={},Stemming={},BuiltinStopWords={},AddStopWords={},KeepStopWords={},SettingsFingerprint={}",
        quote_literal(route.alias()),
        settings.stemming,
        settings.builtin_stopwords,
        quote_literal(
            serde_json::to_string(settings.words(route, false)).expect("stopword strings")
        ),
        quote_literal(
            serde_json::to_string(settings.words(route, true)).expect("stopword strings")
        ),
        quote_literal(settings.fingerprint())
    )
}
pub fn create(name: &str, value: serde_json::Value) -> pg_sys::Oid {
    let settings = pg_ffi::checked(Settings::parse(value));
    if name.contains('\0') {
        pg_ffi::invalid("configuration name contains NUL");
    }
    let parts = Spi::get_one::<Vec<String>>(&format!(
        "SELECT pg_catalog.parse_ident({},true)",
        quote_literal(name)
    ))
    .unwrap_or_else(|e| pg_ffi::invalid(format!("invalid qualified configuration name: {e}")))
    .unwrap_or_default();
    if parts.len() != 2 {
        pg_ffi::invalid("configuration name must be schema-qualified");
    }
    let qualified = quote_qualified_identifier(&parts[0], &parts[1]);
    execute(&format!(
        "CREATE TEXT SEARCH CONFIGURATION {qualified} (PARSER=charabia.parser)"
    ));
    let id = Spi::get_one::<pg_sys::Oid>(&format!(
        "SELECT {}::pg_catalog.regconfig::pg_catalog.oid",
        quote_literal(&qualified)
    ))
    .expect("created configuration OID")
    .expect("created config");
    for route in Route::ALL.into_iter().filter(|r| !r.is_display()) {
        let dict = quote_qualified_identifier(
            &parts[0],
            &format!("_pgcb_{}_{}", id.to_u32(), route.alias()),
        );
        execute(&format!(
            "CREATE TEXT SEARCH DICTIONARY {dict} (TEMPLATE=charabia.template,{})",
            dictionary_parameters(route, &settings)
        ));
        execute(&format!(
            "ALTER TEXT SEARCH CONFIGURATION {qualified} ADD MAPPING FOR {} WITH {dict}",
            pgrx::spi::quote_identifier(route.alias())
        ));
    }
    Snapshot::load(id);
    id
}
pub fn alter(id: pg_sys::Oid, value: serde_json::Value) -> serde_json::Value {
    let settings = pg_ffi::checked(Settings::parse(value));
    let old = Snapshot::for_update(id);
    for (route, d) in Route::ALL
        .into_iter()
        .filter(|r| !r.is_display())
        .zip(&old.dictionaries)
    {
        execute(&format!(
            "ALTER TEXT SEARCH DICTIONARY {} ({})",
            d.qualified_name,
            dictionary_parameters(route, &settings)
        ));
    }
    let updated = Snapshot::load(id);
    serde_json::json!({"settings":updated.settings,"settings_fingerprint":updated.settings.fingerprint(),"analysis_fingerprint":updated.settings.analysis_fingerprint(),"maintenance_required":true,"maintenance":"End old transactions, recompute stored vectors, rebuild affected indexes, and reprepare queries before resuming traffic."})
}
pub fn drop_config(id: pg_sys::Oid) {
    let old = Snapshot::for_update(id);
    execute(&format!(
        "DROP TEXT SEARCH CONFIGURATION {} RESTRICT",
        old.qualified_name()
    ));
    for d in old.dictionaries {
        execute(&format!(
            "DROP TEXT SEARCH DICTIONARY {} RESTRICT",
            d.qualified_name
        ));
    }
}
pub fn info(snapshot: &Snapshot) -> serde_json::Value {
    serde_json::json!({
        "extension":env!("CARGO_PKG_VERSION"),"charabia":"0.10.0","pgrx":"0.19.3","target":env!("PG_CHARABIA_TARGET"),
        "cargo_lock_sha256":env!("PG_CHARABIA_LOCK_SHA256"),"resource_manifest_sha256":env!("PG_CHARABIA_RESOURCES_SHA256"),"test_hooks":cfg!(feature="test-hooks"),
        "resources":serde_json::from_str::<serde_json::Value>(include_str!("../resources/manifest.json")).expect("resource manifest"),
        "settings":snapshot.settings,"settings_fingerprint":snapshot.settings.fingerprint(),"analysis_fingerprint":snapshot.settings.analysis_fingerprint(),
        "configuration_oid":snapshot.id.to_u32(),"configuration_name":snapshot.qualified_name(),"route_count":71,"token_type_count":72,"languages":70,"stopword_languages":52,"stemmer_languages":19,
    })
}
pub fn parameters_from_strings(values: Vec<(String, String)>) -> DictionaryParameters {
    let mut map = std::collections::BTreeMap::new();
    for (key, value) in values {
        let key = key.to_ascii_lowercase();
        if map.insert(key.clone(), value).is_some() {
            pg_ffi::invalid(format!("duplicate dictionary option: {key}"));
        }
    }
    if map.keys().any(|x| {
        !matches!(
            x.as_str(),
            "route"
                | "stemming"
                | "builtinstopwords"
                | "addstopwords"
                | "keepstopwords"
                | "settingsfingerprint"
        )
    }) {
        pg_ffi::invalid("unknown dictionary option");
    }
    let route = map
        .remove("route")
        .and_then(|x| Route::from_alias(&x))
        .filter(|r| !r.is_display())
        .unwrap_or_else(|| pg_ffi::invalid("a valid language Route is required"));
    fn boolean(map: &mut std::collections::BTreeMap<String, String>, key: &str) -> bool {
        match map.remove(key).as_deref().unwrap_or("true") {
            "true" | "t" | "on" | "yes" | "1" => true,
            "false" | "f" | "off" | "no" | "0" => false,
            _ => pg_ffi::invalid(format!("invalid dictionary boolean: {key}")),
        }
    }
    let stemming = boolean(&mut map, "stemming");
    let builtin_stopwords = boolean(&mut map, "builtinstopwords");
    fn words(map: &mut std::collections::BTreeMap<String, String>, key: &str) -> Vec<String> {
        let s = map.remove(key).unwrap_or_else(|| "[]".to_owned());
        if s.len() > crate::analysis::MAX_STOP_WORDS_JSON_BYTES {
            pg_ffi::fail(
                pgrx::PgSqlErrorCode::ERRCODE_PROGRAM_LIMIT_EXCEEDED,
                "dictionary stopword JSON exceeds 1048576 bytes",
            );
        }
        serde_json::from_str(&s).unwrap_or_else(|e| pg_ffi::invalid(format!("invalid {key}: {e}")))
    }
    let add = words(&mut map, "addstopwords");
    let keep = words(&mut map, "keepstopwords");
    let settings_fingerprint = map.remove("settingsfingerprint").unwrap_or_default();
    if settings_fingerprint.len() != 64
        || !settings_fingerprint.bytes().all(|b| b.is_ascii_hexdigit())
    {
        pg_ffi::invalid("SettingsFingerprint must contain 64 hexadecimal digits");
    }
    DictionaryParameters {
        route,
        stemming,
        builtin_stopwords,
        add,
        keep,
        settings_fingerprint,
    }
}
