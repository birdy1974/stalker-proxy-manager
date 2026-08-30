#!/usr/bin/env node
/*
Syntax-check the JavaScript inside the Jinja templates (and app/static/js).

Why this exists: a broken template script is invisible to every Python test -
Jinja parses, the page renders, the API answers 200, and the GUI is a blank table
until a human opens it. The tables on the sources/portals pages are built
entirely in JS, so the one gate that catches a typo there is a real parser run
over the extracted script.

It is deliberately a *syntax* check with a text-level Jinja pass, not a render:

  * `{{ expr }}`             -> `0`      (any expression is fine for parsing)
  * `{% if %}A{% else %}B…`  -> `A`      (keep one branch; the rendered page has one)
  * `{% for %}body{% endfor %}` -> `body`
  * other `{% … %}`          -> removed

so a `{% if %}` that wraps a whole statement does not produce a phantom error.
No assertion about behaviour belongs in here: there is no DOM, and pretending
otherwise would make this script the thing that needs testing.

Usage:  node dev/check-js.js [file …]        (default: app/templates/*.html + app.js)
Exit:   0 all parsed, 1 something did not.
*/
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm');

const DEFAULTS = [
  'app/static/js/app.js',
  ...fs.readdirSync('app/templates').filter(f => f.endsWith('.html')).map(f => path.join('app/templates', f)),
];
const files = process.argv.slice(2).length ? process.argv.slice(2) : DEFAULTS;

function stripJinja(src) {
  let s = src;
  // for / if blocks: keep the first branch, drop the tags. Non-greedy on purpose -
  // a template with two `{% if %}`s on one line is one branch each.
  s = s.replace(/\{%-?\s*(if|elif)[^%]*%-?\}/g, '/*jinja*/ void 0; if (true) {')
       .replace(/\{%-?\s*else\s*-?%\}/g, '} else if (false) {')
       .replace(/\{%-?\s*(elif)[^%]*%-?\}/g, '} else if (true) {')
       .replace(/\{%-?\s*endif\s*-?%\}/g, '}');
  s = s.replace(/\{%-?\s*(for|while)[^%]*%-?\}/g, '/*jinja*/ for (const x of []) {')
       .replace(/\{%-?\s*end(for|while)\s*-?%\}/g, '}');
  s = s.replace(/\{%-?\s*(block|raw|autoescape)[^%]*%-?\}/g, '/*jinja*/ void 0; if (true) {')
       .replace(/\{%-?\s*end(block|raw|autoescape)\s*-?%\}/g, '}');
  // `{{ ... }}` in a template-literal or an attribute is an expression we cannot
  // know; `0` parses everywhere a value is allowed
  s = s.replace(/\{\{[\s\S]*?\}\}/g, '0');
  // `{% macro %}`, `{% set %}`, `{% extends %}` … and anything left
  s = s.replace(/\{%[^%]*%\}/g, '/*jinja*/ void 0;');
  return s;
}

function* scriptsOf(file) {
  const src = fs.readFileSync(file, 'utf8');
  if (file.endsWith('.js')) { yield [1, src]; return; }
  const re = /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g;
  let m, n = 0;
  while ((m = re.exec(src))) yield [++n, m[1]];
}

let bad = 0, checked = 0;
for (const file of files) {
  const blocks = [...scriptsOf(file)];
  if (!blocks.length) continue;
  for (const [n, body] of blocks) {
    const label = file.endsWith('.js') ? file : `${file}#script${n}`;
    const js = stripJinja(body);
    checked++;
    try {
      new vm.Script(js, { filename: label });
      console.log(`  ok   ${label}`);
    } catch (e) {
      bad++;
      console.log(`  FAIL ${label}: ${e.message}`);
      const at = (e.stack || '').match(new RegExp(`${label.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}:(\\d+)`));
      if (at) {
        const lines = js.split('\n');
        for (let i = Math.max(0, +at[1] - 3); i < Math.min(lines.length, +at[1] + 1); i++)
          console.log(`       ${i + 1 === +at[1] ? '>>' : '  '} ${String(i + 1).padStart(4)} | ${lines[i].slice(0, 160)}`);
      }
    }
  }
}
if (bad) console.error(`\n${bad} of ${checked} script block(s) do not parse`);
process.exit(bad ? 1 : 0);
