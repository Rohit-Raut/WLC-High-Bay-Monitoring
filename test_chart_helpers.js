#!/usr/bin/env node
/**
 * Self-check for the array min/max helpers in features/dashboard/*.js
 *
 *     node test_chart_helpers.js
 *
 * Math.max(...arr) throws "RangeError: Maximum call stack size exceeded" past
 * ~124k elements. Those calls sat at top-level scope, so once the archive grew
 * past ~36k records the whole dashboard script died before rendering and the
 * page came up with no charts at all. _arrMax/_arrMin loop instead.
 *
 * Checks, per dashboard JS file:
 *   1. the helpers agree with Math.max/min on arrays small enough to compare;
 *   2. they survive an array far larger than the spread ceiling;
 *   3. no `Math.max(...x)` / `Math.min(...x)` spread has crept back in.
 */

const fs = require('fs');
const path = require('path');

const FILES = [
  'features/dashboard/chart_interactions_local.js',
  'features/dashboard/chart_interactions.js',
];
const HUGE = 300000;   // well past Chrome's ~124k spread ceiling
let failures = 0;

function check(name, cond) {
  if (cond) {
    console.log(`  ok  ${name}`);
  } else {
    console.log(`  FAIL ${name}`);
    failures++;
  }
}

// Confirm the ceiling this test exists to defend against is real in this engine.
let spreadThrows = false;
try {
  Math.max(...new Array(HUGE).fill(1));
} catch (e) {
  spreadThrows = e instanceof RangeError;
}
check(`Math.max spread on ${HUGE} elements still throws RangeError ` +
      `(if this fails the engine changed, not the code)`, spreadThrows);

for (const rel of FILES) {
  const file = path.join(__dirname, rel);
  const src = fs.readFileSync(file, 'utf8');
  console.log(`\n${rel}`);

  // Pull the two helpers out of the real file — no copy of the logic here.
  const decls = src.match(/function _arr(?:Max|Min)\s*\([\s\S]*?\n}/g) || [];
  check('both helpers defined in the file', decls.length === 2);
  if (decls.length !== 2) continue;
  const { _arrMax, _arrMin } = new Function(
    `${decls.join('\n')}\nreturn { _arrMax, _arrMin };`)();

  const small = [3, -1, 0, 42, 7, -99, 42];
  check('_arrMax matches Math.max on a small array',
        _arrMax(small) === Math.max(...small));
  check('_arrMin matches Math.min on a small array',
        _arrMin(small) === Math.min(...small));
  check('_arrMax of a single element', _arrMax([5]) === 5);
  check('_arrMax of [] is -Infinity (callers guard on .length)',
        _arrMax([]) === -Infinity);
  check('_arrMin of [] is  Infinity (callers guard on .length)',
        _arrMin([]) === Infinity);

  // The actual regression: a 300k array, with the extremes at both ends so a
  // partial scan can't pass by accident.
  const huge = new Array(HUGE).fill(1);
  huge[0] = 999999;
  huge[HUGE - 1] = -999999;
  let ok = false;
  try {
    ok = _arrMax(huge) === 999999 && _arrMin(huge) === -999999;
  } catch (e) {
    console.log(`       threw: ${e.constructor.name}: ${e.message}`);
  }
  check(`_arrMax/_arrMin handle ${HUGE} elements without throwing`, ok);

  // Guard the pattern itself. Two-arg Math.max(32, x) is fine — only spread is not.
  const spreads = (src.match(/Math\.(?:max|min)\(\.\.\./g) || []).length;
  check('no Math.max(...)/Math.min(...) spread remains', spreads === 0);
}

console.log(failures === 0
  ? '\nall checks passed'
  : `\n${failures} CHECK(S) FAILED`);
process.exit(failures === 0 ? 0 : 1);
