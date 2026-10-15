'use strict';
// Signature help for OType, as pure logic (no vscode import).
//
// OType is in prefix notation with a fixed arity: `PL.SET I AMP.VOL RRAND 70 `
// is PL.SET s p v, whose third argument is RRAND a b, which has one argument
// still to come.  signatureAt() tokenizes the line up to the cursor, walks the
// tokens with a stack of unfinished operations and returns the innermost one.

// Language modifiers and declarations that take arguments before the ':'.
// The language is case-insensitive and its canonical written form is lower case (the checker
// prints everything in lower case): names are looked up in lower case, whatever was typed.
const LANG = {
  if: { params: ['c'], doc: 'run the line or block when c is true' },
  elif: { params: ['c'], doc: 'another condition, right after an if/elif at the same indentation' },
  l: { params: ['a', 'b'], doc: 'loop i from a to b inclusive; the bounds are constant' },
  prob: { params: ['p'], doc: 'run with p percent probability' },
  every: { params: ['n'], doc: 'run once every n passes over this line' },
  skip: { params: ['n'], doc: 'run every time except once every n passes' },
  del: { params: ['n'], doc: 'reserved: run n steps later' },
  param: { params: ['name', 'default', 'min', 'max'], doc: 'a setting editable on the unit' },
};
const NO_HELP = new Set(['def', 'const', 'menu', 'on', 'confirm', 'otype', 'input', 'else', 'each',
  'break', 'stop']);

/** Index the output of `otype.py ops --json`: lower-case name or alias -> entry. */
function buildRegistry(opsList) {
  const map = new Map();
  for (const e of opsList) {
    if (e.category === 'KEYWORD' || e.category === 'VARIABLE') {
      // a keyword that is also an operation (trk, steps, step, seed, on) keeps the operation
      if (!map.has(e.name.toLowerCase())) map.set(e.name.toLowerCase(), e);
      continue;
    }
    map.set(e.name.toLowerCase(), e);
    for (const a of e.alias || []) map.set(a.toLowerCase(), e);
  }
  return map;
}

/** [min, max] number of arguments of a registry entry (max null = unbounded). */
function arity(e) {
  if (typeof e.minArgs === 'number') {
    return [e.minArgs, e.maxArgs === undefined ? e.args.length : e.maxArgs];
  }
  let min = 0;
  let max = 0;
  for (const raw of e.args || []) {
    const k = raw.toLowerCase();
    if (k.startsWith('[')) { max = max === null ? null : max + 1; continue; }
    min += 1;
    if (k.startsWith('rest(')) max = null;
    else if (k === 'tracks') max = max === null ? null : max + 8;
    else if (k === 'stepsel') max = max === null ? null : max + 2;
    else max = max === null ? null : max + 1;
  }
  return [min, max];
}

/**
 * Split a line (already cut at the cursor) into tokens.
 * Returns {tokens, open, comment}: `open` = the cursor is inside a string or a list,
 * `comment` = a '#' comment started before the cursor.
 * Token: {kind: 'word'|'str'|'list'|'sym', text, start, end}.
 */
function tokenize(text) {
  const tokens = [];
  let i = 0;
  const n = text.length;
  while (i < n) {
    const c = text[i];
    if (c === ' ' || c === '\t') { i++; continue; }
    if (c === '#') return { tokens, open: false, comment: true };
    if (c === '"') {
      const j = text.indexOf('"', i + 1);
      if (j < 0) return { tokens, open: true, comment: false };
      tokens.push({ kind: 'str', text: text.slice(i, j + 1), start: i, end: j + 1 });
      i = j + 1;
    } else if (c === '{') {
      const j = text.indexOf('}', i);
      if (j < 0) return { tokens, open: true, comment: false };
      tokens.push({ kind: 'list', text: text.slice(i, j + 1), start: i, end: j + 1 });
      i = j + 1;
    } else if (c === ':' || c === '|') {
      tokens.push({ kind: 'sym', text: c, start: i, end: i + 1 });
      i++;
    } else {
      let j = i;
      while (j < n) {
        const ch = text[j];
        if (ch === ' ' || ch === '\t' || ch === '"' || ch === '{' || ch === '}' || ch === '|') break;
        if (ch === ':') {
          // ':' glued between two digits (1:4) is a condition, not a modifier end
          if (/^[0-9]+$/.test(text.slice(i, j)) && j + 1 < n && /[0-9]/.test(text[j + 1])) {
            j++;
            continue;
          }
          break;
        }
        j++;
      }
      if (j === i) j = i + 1; // a stray '}' : skip it
      tokens.push({ kind: 'word', text: text.slice(i, j), start: i, end: j });
      i = j;
    }
  }
  return { tokens, open: false, comment: false };
}

function paramOffsets(label, params) {
  const out = [];
  let from = label.indexOf(' ') < 0 ? 0 : label.indexOf(' ');
  for (const p of params) {
    const k = label.indexOf(p, from);
    if (k < 0) { out.push(null); continue; }
    out.push([k, k + p.length]);
    from = k + p.length;
  }
  return out;
}

/**
 * Signature help at `col` (0-based character offset) of `line`.
 *   registry  Map from buildRegistry()
 *   funcs     Map lower-case name -> {name, args: [...]} of the user functions (may be omitted)
 * Returns null, or {label, params, offsets, activeParam (0-based), doc, name, kind}.
 */
function signatureAt(line, col, registry, funcs) {
  funcs = funcs || new Map();
  const { tokens: all, open, comment } = tokenize(line.slice(0, col));
  if (open || comment) return null;
  let toks = all;
  // a word still being typed (no space after it yet) is not an argument yet
  const last = toks[toks.length - 1];
  if (last && last.kind === 'word' && last.end === col) toks = toks.slice(0, -1);
  // everything up to the last top-level ':' is modifiers (IF, L, PROB, TRK 3 4 ...)
  let cut = -1;
  for (let k = 0; k < toks.length; k++) if (toks[k].kind === 'sym' && toks[k].text === ':') cut = k;
  toks = toks.slice(cut + 1);
  if (!toks.length) return null;

  const stack = [];
  const fill = () => {
    // a value was completed: it fills the innermost frame, which may complete in turn
    for (;;) {
      const top = stack[stack.length - 1];
      if (!top) return;
      top.filled += 1;
      if (top.max !== null && top.filled >= top.max) stack.pop();
      else return;
    }
  };
  const push = (f) => { f.filled = 0; stack.push(f); };

  for (let k = 0; k < toks.length; k++) {
    const t = toks[k];
    if (t.kind === 'sym') {
      if (t.text === '|') {
        const top = stack[stack.length - 1];
        if (top && top.kind === 'user') top.bar = true;
      }
      continue;
    }
    if (t.kind !== 'word') { fill(); continue; }          // string or list
    const W = t.text.toLowerCase();
    const atStart = stack.length === 0;
    if (atStart && k === 0 && NO_HELP.has(W)) return null;
    if (atStart && Object.prototype.hasOwnProperty.call(LANG, W)) {
      push({ kind: 'lang', name: W, params: LANG[W].params, min: LANG[W].params.length,
        max: LANG[W].params.length, doc: LANG[W].doc, label: W + ' ' + LANG[W].params.join(' ') });
      continue;
    }
    const top = stack[stack.length - 1];
    const op = registry.get(W);
    const fn = funcs.get(W);
    if (op && !(op.category === 'KEYWORD' || op.category === 'VARIABLE')) {
      // `STEPS HAS.TRIG`: a predicate without its argument completes the STEPS frame
      if (top && top.name === 'steps' && op.predicate) {
        stack.pop();
        fill();
        continue;
      }
      let [min, max] = arity(op);
      let params = op.argnames && op.argnames.length ? op.argnames : [];
      let label = op.signature;
      // `FLIP x` : the getter starting a statement is a setter
      if (op.settable && k === 0) {
        min = 1;
        max = 1;
        params = ['x'];
        label = op.name + ' x';
      }
      if (max === 0) { fill(); continue; }                // a value such as AMP.VOL, TOSS
      push({ kind: 'op', name: op.name, params, min, max, doc: op.doc, label });
      continue;
    }
    if (fn) {
      const a = fn.args || [];
      if (!a.length) { fill(); continue; }                // NAME alone
      push({ kind: 'user', name: fn.name, params: a, min: a.length, max: a.length, bar: false,
        doc: fn.doc || '', label: fn.name + ' | ' + a.join(' ') });
      continue;
    }
    fill();                                               // number, variable, CONST, condition...
  }
  const top = stack[stack.length - 1];
  if (!top) return null;
  const n = top.params.length;
  let active = n ? Math.min(top.filled, n - 1) : 0;
  if (top.kind === 'user' && !top.bar) active = -1;       // `NAME ` : the '|' comes first
  const label = top.label;
  return {
    label, params: top.params, offsets: paramOffsets(label, top.params),
    activeParam: active, doc: top.doc, name: top.name, kind: top.kind,
  };
}

module.exports = { signatureAt, tokenize, buildRegistry, arity, LANG };
