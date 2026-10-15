'use strict';
// Run: node editors/vscode/test/sighelp.test.js   (plain asserts, no dependencies)
// Registry source: `python3 tools/otype.py ops --json` (spawned here), so the test follows
// the real registry; the user functions are given by hand.
const assert = require('assert');
const path = require('path');
const { spawnSync } = require('child_process');
const { signatureAt, tokenize, buildRegistry, arity } = require('../lib/sighelp.js');

const tool = path.join(__dirname, '..', 'checker', 'tools', 'otype.py');
let stdout = process.env.OTYPE_OPS_JSON;
if (!stdout) {
  const r = spawnSync('python3', [tool, 'ops', '--json'], { encoding: 'utf8' });
  assert.strictEqual(r.status, 0, 'ops --json failed: ' + (r.stderr || (r.error && r.error.message)));
  stdout = r.stdout;
}
const registry = buildRegistry(JSON.parse(stdout));
// the checker gives user names in lower case (the language is case-insensitive)
const funcs = new Map([
  ['hats', { name: 'hats', args: ['track', 'folder', 'dens'] }],
  ['reset_all', { name: 'reset_all', args: [] }],
]);

let count = 0;
function at(line, col) {
  return signatureAt(line, col === undefined ? line.length : col, registry, funcs);
}
function expect(line, name, active, label) {
  const s = at(line);
  assert.ok(s, 'expected help for: ' + JSON.stringify(line));
  assert.strictEqual(s.name, name, JSON.stringify(line) + ' -> ' + s.name);
  assert.strictEqual(s.activeParam, active, JSON.stringify(line) + ' active ' + s.activeParam);
  if (label !== undefined) assert.strictEqual(s.label, label);
  count++;
}
function none(line, col) {
  const s = at(line, col);
  assert.strictEqual(s, null, 'expected no help for ' + JSON.stringify(line) + ' got ' + (s && s.label));
  count++;
}

// --- the cases of the brief (activeParam is 0-based: "param 2" = index 1)
expect('pl.set i amp.vol rrand 70 ', 'rrand', 1, 'rrand a b');
expect('l 1 16: prob 35: tr.add ', 'tr.add', 0, 'tr.add s');
none('cond.set 3 1:4 ');

// --- plain statements
expect('pl.set ', 'pl.set', 0, 'pl.set s p v');
expect('pl.set 1 ', 'pl.set', 1);
expect('pl.set 1 amp.vol ', 'pl.set', 2);
none('pl.set 1 amp.vol 100 ');
expect('  pl.set i amp.vol add 1 ', 'add', 1, 'add x y');   // indentation does not matter
none('pl.set i amp.vol add 1 2 ');                         // add fills the third argument
expect('pl.set 1 add 1 2 ', 'pl.set', 2);                  // add complete: back to pl.set
expect('pl.set 1 add 1 ', 'add', 1);

// --- a word still being typed is not an argument yet
expect('tr.add 5', 'tr.add', 0);
expect('pl.set 1 amp.v', 'pl.set', 1);

// --- nested operations, aliases, the ternary
expect('a + 1 ', 'add', 1);
expect('a ? ', '?', 0, '? c a b');
expect('a ? x ', '?', 1);
expect('a ? x 1 ', '?', 2);
none('a ? x 1 2 ');
expect('a lim add x 10 ', 'lim', 1);
expect('a lim add x ', 'add', 1);
expect('a lim add x 10 0 ', 'lim', 2, 'lim x lo hi');
expect('a rrand rnd 4 ', 'rrand', 1);

// --- modifiers: stripped when closed, shown while open
none('l 1 16: ');
none('if x: ');
expect('if ', 'if', 0, 'if c');
expect('l 1 ', 'l', 1, 'l a b');
expect('l 1 16: prob ', 'prob', 0);
expect('l 1 16: every add x ', 'add', 1);
expect('l 1 16: if eq i ', 'eq', 1);
expect('trk 3 4: pl.set ', 'pl.set', 0);
expect('steps has.trig: each: pl.set i ', 'pl.set', 1);
expect('each: smp.set i ', 'smp.set', 1);

// --- conditions glued with ':' are not modifier ends
expect('cond.set 3 ', 'cond.set', 1);
expect('l 1 4: cond.set i ', 'cond.set', 1);
none('l 1 4: cond.set i 1:4 ');
none('cond.set i 50% ');
expect('cond.set 1:4 ', 'cond.set', 1);

// --- user functions: NAME | args
expect('hats ', 'hats', -1, 'hats | track folder dens');
expect('hats | ', 'hats', 0);
expect('hats | 3 ', 'hats', 1);
expect('hats | 3 "AUDIO/X" ', 'hats', 2);
none('hats | 3 "AUDIO/X" 40 ');
expect('hats | 3 "A:B # c" rrand 1 ', 'rrand', 1);
none('reset_all ');
expect('a hats | ', 'hats', 0);
expect('l 1 8: hats | i ', 'hats', 1);

// --- variadic and optional arguments
expect('trk ', 'trk', 0);
expect('trk 3 4 ', 'trk', 0, 'trk t...');
expect('trk add 1 ', 'add', 1);
expect('dir "X" ', 'dir', 1);
expect('match "a*" ', 'match', 0);
expect('steps ', 'steps', 0);
none('steps has.trig ');
expect('steps 1 ', 'steps', 0);
// the settable getter starting a statement takes its value
expect('flip ', 'flip', 0, 'flip x');
none('a flip ');                                          // a read: a value, no help

// --- no help
none('');
none('def hats | ');
none('menu "A" >> ');
none('const x ');
none('pl.set 1 # amp.vol ');          // inside a comment
none('msg "abc ');                    // inside a string
none('slots.add {1 2 ');              // inside a list
expect('pl.set 1 "#" ', 'pl.set', 2);  // '#' in a string is not a comment
expect('param dens 40 ', 'param', 2);

// --- offsets point at the parameter names in the label
{
  const s = at('pl.set 1 ');
  assert.strictEqual(s.label.slice(s.offsets[1][0], s.offsets[1][1]), 'p');
  const h = at('hats | 3 ');
  assert.strictEqual(h.label.slice(h.offsets[1][0], h.offsets[1][1]), 'folder');
  count++;
}

// --- cursor in the middle of a line only reads what is left of it
{
  const s = at('pl.set 1 amp.vol 100', 'pl.set 1 '.length);
  assert.strictEqual(s.activeParam, 1);
  count++;
}

// --- the language is case-insensitive: upper and mixed case give the same help, in lower case
expect('PL.SET I AMP.VOL RRAND 70 ', 'rrand', 1, 'rrand a b');
expect('Pl.Set 1 ', 'pl.set', 1, 'pl.set s p v');
expect('l 1 16: Prob 35: Tr.Add ', 'tr.add', 0, 'tr.add s');
expect('HATS | 3 "AUDIO/X" ', 'hats', 2, 'hats | track folder dens');
expect('Hats ', 'hats', -1, 'hats | track folder dens');
expect('A LiM aDd X ', 'add', 1);
expect('EACH: SMP.SET I ', 'smp.set', 1);
none('Cond.Set I 1:4 ');
none('MENU "A" >> ');
none('Def Hats | ');
// a user function named like an Object.prototype member is not a language word
none('constructor ');
{
  const s = at('PL.SET 1 ');
  assert.strictEqual(s.label, 'pl.set s p v');
  assert.strictEqual(at('Param Dens ').label, 'param name default min max');
  assert.strictEqual(at('PARAM ').label, 'param name default min max');
  count += 3;
}

// --- audio operations (audio extension)
expect('flex.load 1 ', 'flex.load', 1, 'flex.load n f');
expect('norm file.at 1 ', 'norm', 1, 'norm f db');
expect('cat.save ', 'cat.save', 0, 'cat.save name');
none('input sample ');

// --- tokenizer
{
  const t = tokenize('L 1 16: TR.ADD "a b" 1:4 {1 2}  # x');
  assert.deepStrictEqual(t.tokens.map((x) => x.text),
    ['L', '1', '16', ':', 'TR.ADD', '"a b"', '1:4', '{1 2}']);
  assert.strictEqual(t.comment, true);
  // the kinds of `ops --json` are lower case (older upper-case spellings still read)
  assert.deepStrictEqual(arity({ args: ['step', 'param'] }), [2, 2]);
  assert.deepStrictEqual(arity({ args: ['rest(str)'] }), [1, null]);
  assert.deepStrictEqual(arity({ args: ['str', '[deep]'] }), [1, 2]);
  assert.deepStrictEqual(arity({ args: ['tracks'] }), [1, 8]);
  assert.deepStrictEqual(arity({ args: ['STEP', 'PARAM'] }), [2, 2]);
  assert.deepStrictEqual(arity({ args: ["REST('STR')"] }), [1, null]);
  count += 6;
}

console.log('sighelp: ' + count + ' checks passed');
