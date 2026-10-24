\set ON_ERROR_STOP on
BEGIN;
CREATE TEMP TABLE headline_assertions(label text PRIMARY KEY);
CREATE FUNCTION pg_temp.assert_headline(ok boolean, label text) RETURNS void LANGUAGE plpgsql AS $fn$
BEGIN
  IF ok IS DISTINCT FROM true THEN RAISE EXCEPTION 'headline assertion: %', label; END IF;
  INSERT INTO headline_assertions VALUES(label);
END
$fn$;

-- Source: PG18 exact/prefix core matching plus the independently preserved
-- general-convergence Linux probe; expected source bytes are intentionally literal.
SELECT pg_temp.assert_headline(
  ts_headline('public.charabia','CAFÉ café','cafe'::tsquery,'HighlightAll=true')=
    '<b>CAFÉ</b> <b>café</b>',
  'final normalization matches and emits exact original Unicode sequences');
SELECT pg_temp.assert_headline(
  ts_headline('public.charabia','Running runners run','run:*'::tsquery,'HighlightAll=true')=
    '<b>Running</b> <b>runners</b> <b>run</b>',
  'prefix matches final lexeme and highlights each full raw token');
SELECT pg_temp.assert_headline(
  ts_headline('public.charabia','<script>x</script> CAFÉ & cafe','cafe'::tsquery,'HighlightAll=true')=
    '&lt;script&gt;x&lt;/script&gt; <b>CAFÉ</b> &amp; <b>cafe</b>',
  'escape covers source HTML without treating tags as parser markup');
SELECT pg_temp.assert_headline(
  ts_headline('public.charabia','<script>x</script> CAFÉ & cafe','cafe'::tsquery,
    'HighlightAll=true,Escape=false')='<script>x</script> <b>CAFÉ</b> & <b>cafe</b>',
  'explicit raw rendering leaves the source unescaped');
SELECT pg_temp.assert_headline(
  ts_headline('public.charabia','CAFÉ & cafe','cafe'::tsquery,
    'HighlightAll=true,StartSel="<mark>",StopSel="</mark>"')=
    '<mark>CAFÉ</mark> &amp; <mark>cafe</mark>',
  'trusted caller markers are distinct from escaped source');
SELECT pg_temp.assert_headline(
  ts_headline('public.charabia','red the fox','red <2> fox'::tsquery,'HighlightAll=true')=
    '<b>red</b> the <b>fox</b>',
  'phrase distance is based on core positions including intervening stop holes');
SELECT pg_temp.assert_headline(
  NOT (to_tsvector('public.charabia','red the fox') @@ 'red <-> fox'::tsquery)
  AND ts_headline('public.charabia','red the fox','red <-> fox'::tsquery,'HighlightAll=true')=
    '<b>red</b> the <b>fox</b>',
  'headline presents operands and does not replace phrase filtering');
SELECT pg_temp.assert_headline(
  ts_headline('public.charabia','', ''::tsquery,'HighlightAll=true')='',
  'empty input and query do not dereference a null entry array');
SELECT pg_temp.assert_headline(
  ts_headline('public.charabia','Café & 中文',''::tsquery,'HighlightAll=true')='Café &amp; 中文',
  'empty query still renders source with no invented matches');
SELECT pg_temp.assert_headline(
  ts_headline('public.charabia',repeat('x',2047)||' café','cafe'::tsquery,'HighlightAll=true')=
    repeat('x',2047)||' <b>café</b>',
  'display-only long raw token survives the core raw-token rejection boundary');
SELECT pg_temp.assert_headline(
  ts_headline('public.charabia','{"CAFÉ":"CAFÉ & cafe","items":["cafe",2]}'::json,
    'cafe'::tsquery,'HighlightAll=true')::jsonb=
    '{"CAFÉ":"<b>CAFÉ</b> &amp; <b>cafe</b>","items":["<b>cafe</b>",2]}'::jsonb,
  'json string callbacks reuse request state without changing object keys');
SELECT pg_temp.assert_headline(
  ts_headline('public.charabia','{"CAFÉ":"CAFÉ & cafe","items":["cafe",2]}'::jsonb,
    'cafe'::tsquery,'HighlightAll=true')=
    '{"CAFÉ":"<b>CAFÉ</b> &amp; <b>cafe</b>","items":["<b>cafe</b>",2]}'::jsonb,
  'jsonb strings use the same match and rendering policy');

-- The same literal operand matches ordinary raw words or a complete structure.
SELECT pg_temp.assert_headline(
  ts_headline('public.charabia_linguistic','alpha 100ms beta','ms'::tsquery,'HighlightAll=true')=
    'alpha 100ms beta'
  AND ts_headline('public.charabia_structured','alpha 100ms beta','ms'::tsquery,'HighlightAll=true')=
    'alpha <b>100ms</b> beta',
  'linguistic has no unit component while structured highlights its whole source');
SELECT pg_temp.assert_headline(
  ts_headline('public.charabia','GET https://Example.com/Running?q=100ms','run'::tsquery,'HighlightAll=true')=
    'GET https://Example.com/<b>Running</b>?q=100ms'
  AND ts_headline('public.charabia_structured','GET https://Example.com/Running?q=100ms','run'::tsquery,'HighlightAll=true')=
    'GET <b>https://Example.com/Running?q=100ms</b>',
  'default linguistic URL word and explicit structured whole URL retain exact source');

SELECT count(*) AS headline_sql_assertions FROM headline_assertions;
ROLLBACK;
