'use strict';
// OType for VS Code: live checking (diagnostics), completion, hover, signature help,
// go to definition and outline.  Plain CommonJS, no dependencies, no build step.
//
// The language knowledge is NOT here: the checker (tools/otype.py, bundled in ./checker or
// pointed to by otype.checkerPath) is run as a child process.
//   check --json --stdin-file DOC FILES...  -> diagnostics, functions, consts
//   ops --json                              -> the operation registry (completion, hover, help)
const vscode = require('vscode');
const cp = require('child_process');
const fs = require('fs');
const path = require('path');
const { signatureAt, buildRegistry } = require('./lib/sighelp.js');

const LANG = 'otype';
const RUN_TIMEOUT_MS = 20000;

let ctx;                       // ExtensionContext
let diagnostics;               // DiagnosticCollection
let statusItem;
let output;                    // OutputChannel "OType"
const results = new Map();     // folder -> {data, versions: Map(fsPath -> doc version), at}
const running = new Map();     // folder -> {child, cancelled, promise}
const timers = new Map();      // folder -> debounce timer
const folderUris = new Map();  // folder -> Set of uri strings that carry diagnostics
let registry = { list: [], map: new Map(), loaded: false };
let warned = false;            // one warning message per session when the checker fails

// ---------------------------------------------------------------- settings / checker
function cfg() {
  return vscode.workspace.getConfiguration('otype');
}

function checkerScript() {
  const p = (cfg().get('checkerPath') || '').trim();
  return p || path.join(ctx.extensionPath, 'checker', 'tools', 'otype.py');
}

function pythonPath() {
  return (cfg().get('pythonPath') || 'python3').trim() || 'python3';
}

function warnOnce(text) {
  if (warned) return;
  warned = true;
  vscode.window.showWarningMessage('OType: ' + text);
}

/** Run the checker.  Resolves {code, stdout, stderr, cancelled}; rejects when it cannot start. */
function runChecker(args, input, holder) {
  return new Promise((resolve, reject) => {
    let child;
    try {
      child = cp.spawn(pythonPath(), [checkerScript()].concat(args), {
        stdio: ['pipe', 'pipe', 'pipe'],
      });
    } catch (e) {
      reject(e);
      return;
    }
    if (holder) holder.child = child;
    const out = [];
    const err = [];
    const timer = setTimeout(() => { try { child.kill(); } catch (e) { /* gone */ } }, RUN_TIMEOUT_MS);
    child.stdout.on('data', (d) => out.push(d));
    child.stderr.on('data', (d) => err.push(d));
    child.on('error', (e) => { clearTimeout(timer); reject(e); });
    child.on('close', (code) => {
      clearTimeout(timer);
      resolve({
        code,
        stdout: Buffer.concat(out).toString('utf8'),
        stderr: Buffer.concat(err).toString('utf8'),
        cancelled: !!(holder && holder.cancelled),
      });
    });
    child.stdin.on('error', () => { /* the child exited before reading: reported by close */ });
    child.stdin.end(input === undefined ? '' : Buffer.from(input, 'utf8'));
  });
}

// ---------------------------------------------------------------- registry
async function loadRegistry(announce) {
  try {
    const r = await runChecker(['ops', '--json']);
    if (r.code !== 0) throw new Error(r.stderr.trim() || 'exit code ' + r.code);
    const list = JSON.parse(r.stdout);
    registry = { list, map: buildRegistry(list), loaded: true };
    if (announce) vscode.window.showInformationMessage('OType: registry reloaded (' + list.length + ' entries).');
  } catch (e) {
    registry = { list: [], map: new Map(), loaded: false };
    warnOnce('cannot read the registry (' + e.message + '). Check the otype.pythonPath and otype.checkerPath settings.');
  }
}

// ---------------------------------------------------------------- checking
function folderOf(doc) {
  return path.dirname(doc.uri.fsPath);
}

function otyFiles(folder, docPath) {
  let names = [];
  try {
    names = fs.readdirSync(folder).filter((n) => /\.oty$/i.test(n));
  } catch (e) { /* folder unreadable: the document alone */ }
  const files = names.map((n) => path.join(folder, n));
  if (docPath && !files.includes(docPath)) files.push(docPath);
  return files.sort();
}

function scheduleCheck(doc, immediate) {
  if (doc.uri.scheme !== 'file' || doc.languageId !== LANG) return;
  const folder = folderOf(doc);
  clearTimeout(timers.get(folder));
  const delay = immediate ? 0 : Math.max(0, cfg().get('debounceMs') || 0);
  timers.set(folder, setTimeout(() => {
    timers.delete(folder);
    runCheck(doc);
  }, delay));
}

/** Check the folder of `doc` with the buffer of `doc`; kills a run still going. */
function runCheck(doc) {
  const folder = folderOf(doc);
  const old = running.get(folder);
  if (old) {
    old.cancelled = true;
    try { old.child.kill(); } catch (e) { /* gone */ }
  }
  const docPath = doc.uri.fsPath;
  const files = otyFiles(folder, docPath);
  const version = doc.version;
  const holder = { child: null, cancelled: false };
  running.set(folder, holder);
  holder.promise = runChecker(['check', '--json', '--stdin-file', docPath].concat(files), doc.getText(), holder)
    .then((r) => {
      if (running.get(folder) === holder) running.delete(folder);
      if (r.cancelled) return results.get(folder) || null;
      let data;
      try {
        data = JSON.parse(r.stdout);
      } catch (e) {
        throw new Error(r.stderr.trim() || 'the checker gave no JSON (exit code ' + r.code + ')');
      }
      const entry = { data, files, versions: new Map([[docPath, version]]), at: Date.now() };
      results.set(folder, entry);
      publish(folder, files, data);
      updateStatus();
      return entry;
    })
    .catch((e) => {
      if (running.get(folder) === holder) running.delete(folder);
      warnOnce('the checker failed (' + e.message + '). Check the otype.pythonPath and otype.checkerPath settings.');
      setStatus('$(warning) OType: checker unavailable', 'The OType checker could not run.');
      return results.get(folder) || null;
    });
  return holder.promise;
}

function publish(folder, files, data) {
  const byFile = new Map(files.map((f) => [f, []]));
  for (const d of data.diagnostics || []) {
    if (!byFile.has(d.file)) byFile.set(d.file, []);
    const line = Math.max(0, d.line - 1);
    const range = new vscode.Range(line, Math.max(0, d.col - 1), line, Math.max(d.col, d.endCol - 1));
    const diag = new vscode.Diagnostic(
      range, d.message,
      d.severity === 'error' ? vscode.DiagnosticSeverity.Error : vscode.DiagnosticSeverity.Warning);
    diag.source = 'otype';
    if (d.func && d.func !== '-') diag.code = d.func;
    byFile.get(d.file).push(diag);
  }
  const now = new Set();
  for (const [file, list] of byFile) {
    const uri = vscode.Uri.file(file);
    diagnostics.set(uri, list);      // an empty list clears the file
    now.add(uri.toString());
  }
  const before = folderUris.get(folder) || new Set();
  for (const u of before) if (!now.has(u)) diagnostics.delete(vscode.Uri.parse(u));
  folderUris.set(folder, now);
}

/** Last result for the folder of `doc`, re-run first when the buffer changed since. */
async function freshResult(doc) {
  if (doc.uri.scheme !== 'file' || doc.languageId !== LANG) return null;
  const folder = folderOf(doc);
  const cur = results.get(folder);
  const inFlight = running.get(folder);
  if (inFlight) return (await inFlight.promise) || cur || null;
  if (cur && cur.versions.get(doc.uri.fsPath) === doc.version) return cur;
  clearTimeout(timers.get(folder));
  timers.delete(folder);
  return (await runCheck(doc)) || cur || null;
}

/** Cached result without re-running (signature help must answer fast). */
function cachedResult(doc) {
  if (doc.uri.scheme !== 'file') return null;
  return results.get(folderOf(doc)) || null;
}

// ---------------------------------------------------------------- status bar
function setStatus(text, tooltip) {
  statusItem.text = text;
  statusItem.tooltip = tooltip || 'OType checker';
}

function updateStatus() {
  const ed = vscode.window.activeTextEditor;
  if (!ed || ed.document.languageId !== LANG) {
    statusItem.hide();
    return;
  }
  const entry = results.get(folderOf(ed.document));
  if (!entry) {
    setStatus('OType …', 'OType: checking');
    statusItem.show();
    return;
  }
  const d = entry.data;
  const errors = (d.diagnostics || []).filter((x) => x.severity === 'error').length;
  const warnings = (d.diagnostics || []).filter((x) => x.severity === 'warning').length;
  const ok = (d.functions || []).filter((f) => f.ok).length;
  const total = (d.functions || []).length;
  const tip = ok + ' of ' + total + ' functions OK, ' + errors + ' error(s), ' + warnings + ' warning(s)';
  if (errors) setStatus('$(error) OType ' + errors + (errors === 1 ? ' error' : ' errors'), tip + ' - click for the Problems panel');
  else setStatus('OType ✓ ' + ok, tip);
  statusItem.show();
}

// ---------------------------------------------------------------- helpers over the result
function functionsOf(entry) {
  return entry ? entry.data.functions || [] : [];
}

function userFuncMap(entry) {
  const m = new Map();
  for (const f of functionsOf(entry)) if (!m.has(f.name)) m.set(f.name, f);
  return m;
}

function funcSignature(f) {
  return f.args.length ? f.name + ' | ' + f.args.join(' ') : f.name;
}

function menuPath(f) {
  return f.menu ? f.menu.join(' >> ') : null;
}

/** The function of `doc` that contains `line` (0-based), from the last result. */
function enclosingFunction(entry, doc, line) {
  let best = null;
  for (const f of functionsOf(entry)) {
    if (f.file !== doc.uri.fsPath || f.line - 1 > line) continue;
    if (!best || f.line > best.line) best = f;
  }
  return best;
}

function wordAt(doc, pos) {
  const r = doc.getWordRangeAtPosition(pos);
  return r ? { range: r, text: doc.getText(r) } : null;
}

function md(parts) {
  const m = new vscode.MarkdownString(parts.filter(Boolean).join('\n\n'));
  m.isTrusted = false;
  return m;
}

function fileLine(f) {
  return path.basename(f.file) + ':' + f.line;
}

// ---------------------------------------------------------------- completion
function opDocumentation(e) {
  const tags = [e.category.toLowerCase()];
  if (e.destructive) tags.push('**destructive**');
  if (e.alias && e.alias.length) tags.push('alias ' + e.alias.map((a) => '`' + a + '`').join(' '));
  return md([e.doc, tags.join(' · ')]);
}

function registryItems() {
  const items = [];
  const seen = new Set();
  // operations first, so that a keyword that is also an operation (trk, steps, step, seed, on)
  // is offered once, as the operation
  const ordered = registry.list.slice().sort((a, b) => {
    const ka = a.category === 'KEYWORD' || a.category === 'VARIABLE' ? 1 : 0;
    const kb = b.category === 'KEYWORD' || b.category === 'VARIABLE' ? 1 : 0;
    return ka - kb;
  });
  for (const e of ordered) {
    if (seen.has(e.name)) continue;
    seen.add(e.name);
    let kind = vscode.CompletionItemKind.Function;
    if (e.category === 'KEYWORD') kind = vscode.CompletionItemKind.Keyword;
    else if (e.category === 'VARIABLE') kind = vscode.CompletionItemKind.Variable;
    const it = new vscode.CompletionItem(e.name, kind);
    it.detail = e.settable ? e.signature + '  /  ' + e.name + ' x' : e.signature;
    it.documentation = opDocumentation(e);
    it.sortText = (kind === vscode.CompletionItemKind.Function ? '2' : kind === vscode.CompletionItemKind.Variable ? '1' : '3') + e.name;
    items.push(it);
  }
  return items;
}

async function provideCompletion(doc, pos) {
  const items = registryItems();
  const entry = await freshResult(doc);
  if (!entry) return items;
  for (const f of functionsOf(entry)) {
    const it = new vscode.CompletionItem(f.name, vscode.CompletionItemKind.Function);
    it.detail = funcSignature(f);
    it.documentation = md([menuPath(f) ? 'menu: ' + menuPath(f) : 'brick (no menu)', fileLine(f)]);
    it.sortText = '0' + f.name;
    if (f.args.length) {
      it.insertText = f.name + ' | ';
      it.command = { command: 'editor.action.triggerParameterHints', title: 'parameter hints' };
    }
    items.push(it);
  }
  for (const c of entry.data.consts || []) {
    const it = new vscode.CompletionItem(c.name, vscode.CompletionItemKind.Constant);
    it.detail = 'const ' + c.name + ' = ' + JSON.stringify(c.value);
    it.documentation = md([path.basename(c.file) + ':' + c.line]);
    it.sortText = '0' + c.name;
    items.push(it);
  }
  const here = enclosingFunction(entry, doc, pos.line);
  if (here) {
    for (const a of here.args) {
      const it = new vscode.CompletionItem(a, vscode.CompletionItemKind.Variable);
      it.detail = 'argument of ' + here.name;
      it.sortText = '0' + a;
      items.push(it);
    }
    for (const p of here.params) {
      const it = new vscode.CompletionItem(p.name, vscode.CompletionItemKind.Property);
      it.detail = 'param ' + p.name + ' ' + p.default + ' ' + p.min + ' ' + p.max;
      it.sortText = '0' + p.name;
      items.push(it);
    }
  }
  return items;
}

// ---------------------------------------------------------------- hover
async function provideHover(doc, pos) {
  const w = wordAt(doc, pos);
  if (!w) return null;
  // case-insensitive language, lower-case canonical form: look names up in lower case
  const W = w.text.toLowerCase();
  const entry = await freshResult(doc);
  // the arguments and params of the function we are in hide nothing: they cannot share a name
  const here = entry ? enclosingFunction(entry, doc, pos.line) : null;
  if (here) {
    if (here.args.includes(W)) {
      return new vscode.Hover(md(['`' + W + '`: argument of `' + funcSignature(here) + '`']), w.range);
    }
    const p = here.params.find((x) => x.name === W);
    if (p) {
      return new vscode.Hover(md(['```otype\nparam ' + p.name + ' ' + p.default + ' ' + p.min + ' ' + p.max + '\n```',
        'setting of `' + here.name + '`, editable on the unit']), w.range);
    }
  }
  const f = entry ? userFuncMap(entry).get(W) : null;
  if (f) {
    const parts = ['```otype\ndef ' + funcSignature(f) + '\n```'];
    const mp = menuPath(f);
    parts.push(mp ? 'menu: **' + mp + '**' : 'brick (no menu entry)');
    const facts = [];
    if (f.cost !== null && f.cost !== undefined) facts.push('cost ' + f.cost);
    if (f.destructive) facts.push('**destructive**');
    if (f.returns) facts.push('returns a value');
    if (!f.ok) facts.push('refused: see the Problems panel');
    if (facts.length) parts.push(facts.join(' · '));
    parts.push(fileLine(f));
    return new vscode.Hover(md(parts), w.range);
  }
  const c = entry ? (entry.data.consts || []).find((x) => x.name === W) : null;
  if (c) {
    return new vscode.Hover(md(['```otype\nconst ' + c.name + ' ' + JSON.stringify(c.value) + '\n```',
      path.basename(c.file) + ':' + c.line]), w.range);
  }
  const e = registry.map.get(W);
  if (e) {
    const sig = e.settable ? e.signature + '  /  ' + e.name + ' x' : e.signature;
    const parts = ['```otype\n' + sig + '\n```', e.doc];
    const tags = [e.category.toLowerCase()];
    if (e.destructive) tags.push('**destructive**');
    if (e.stmt_only) tags.push('statement only');
    if (e.alias && e.alias.length) tags.push('alias ' + e.alias.map((a) => '`' + a + '`').join(' '));
    if (W !== e.name) tags.push('= `' + e.name + '`');
    parts.push(tags.join(' · '));
    return new vscode.Hover(md(parts), w.range);
  }
  return null;
}

// ---------------------------------------------------------------- signature help
function provideSignatureHelp(doc, pos) {
  if (!registry.loaded) return null;
  const entry = cachedResult(doc);
  const funcs = new Map();
  for (const [name, f] of userFuncMap(entry)) funcs.set(name, { name, args: f.args });
  const s = signatureAt(doc.lineAt(pos.line).text, pos.character, registry.map, funcs);
  if (!s) return null;
  const info = new vscode.SignatureInformation(s.label, md([s.doc]));
  info.parameters = s.params.map((p, i) => new vscode.ParameterInformation(s.offsets[i] || p));
  const help = new vscode.SignatureHelp();
  help.signatures = [info];
  help.activeSignature = 0;
  help.activeParameter = s.activeParam < 0 ? s.params.length : s.activeParam;
  return help;
}

// ---------------------------------------------------------------- definition / symbols
async function provideDefinition(doc, pos) {
  const w = wordAt(doc, pos);
  if (!w) return null;
  const W = w.text.toLowerCase();
  const entry = await freshResult(doc);
  if (!entry) return null;
  const target = userFuncMap(entry).get(W) || (entry.data.consts || []).find((x) => x.name === W);
  if (!target) return null;
  const uri = vscode.Uri.file(target.file);
  let col = 0;
  try {
    const d = await vscode.workspace.openTextDocument(uri);
    const text = d.lineAt(target.line - 1).text;
    const k = text.toLowerCase().indexOf(W);
    if (k >= 0) col = k;
  } catch (e) { /* the position of the line is enough */ }
  return new vscode.Location(uri, new vscode.Range(target.line - 1, col, target.line - 1, col + W.length));
}

async function provideSymbols(doc) {
  const entry = await freshResult(doc);
  if (!entry) return [];
  const mine = [];
  for (const f of functionsOf(entry)) {
    if (f.file === doc.uri.fsPath) mine.push({ kind: vscode.SymbolKind.Function, name: f.name, line: f.line, detail: menuPath(f) || 'brick' });
  }
  for (const c of entry.data.consts || []) {
    if (c.file === doc.uri.fsPath) mine.push({ kind: vscode.SymbolKind.Constant, name: c.name, line: c.line, detail: JSON.stringify(c.value) });
  }
  mine.sort((a, b) => a.line - b.line);
  const out = [];
  mine.forEach((s, i) => {
    const start = Math.min(s.line - 1, doc.lineCount - 1);
    let end = (i + 1 < mine.length ? mine[i + 1].line - 1 : doc.lineCount) - 1;
    while (end > start && doc.lineAt(end).isEmptyOrWhitespace) end--;
    end = Math.min(Math.max(end, start), doc.lineCount - 1);
    const range = new vscode.Range(start, 0, end, doc.lineAt(end).text.length);
    const text = doc.lineAt(start).text;
    const k = text.toLowerCase().indexOf(s.name);
    const sel = k >= 0 ? new vscode.Range(start, k, start, k + s.name.length) : new vscode.Range(start, 0, start, text.length);
    out.push(new vscode.DocumentSymbol(s.name, s.detail, s.kind, range, sel));
  });
  return out;
}

// ---------------------------------------------------------------- commands
async function commandCheckFolder() {
  const ed = vscode.window.activeTextEditor;
  let folder = null;
  let doc = null;
  if (ed && ed.document.languageId === LANG && ed.document.uri.scheme === 'file') {
    doc = ed.document;
    folder = folderOf(doc);
  } else if (vscode.workspace.workspaceFolders && vscode.workspace.workspaceFolders.length) {
    folder = vscode.workspace.workspaceFolders[0].uri.fsPath;
  }
  if (!folder) {
    vscode.window.showInformationMessage('OType: open an .OTY file or a folder first.');
    return;
  }
  const list = otyFiles(folder, doc ? doc.uri.fsPath : null);
  output.show(true);
  output.appendLine('--- OType: check -v ' + folder + ' (' + list.length + ' file(s)) ---');
  if (!list.length) {
    output.appendLine('no .OTY file in this folder');
    return;
  }
  try {
    const args = doc ? ['check', '-v', '--stdin-file', doc.uri.fsPath].concat(list) : ['check', '-v'].concat(list);
    const r = await runChecker(args, doc ? doc.getText() : undefined);
    output.appendLine((r.stdout + r.stderr).replace(/\s+$/, ''));
    output.appendLine('exit code ' + r.code);
  } catch (e) {
    output.appendLine('the checker could not run: ' + e.message);
    warnOnce('the checker failed (' + e.message + ').');
  }
}

// ---------------------------------------------------------------- activation
function activate(context) {
  ctx = context;
  diagnostics = vscode.languages.createDiagnosticCollection('otype');
  output = vscode.window.createOutputChannel('OType');
  statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 10);
  statusItem.command = 'workbench.actions.view.problems';
  const selector = { language: LANG };

  context.subscriptions.push(
    diagnostics, output, statusItem,
    vscode.commands.registerCommand('otype.checkFolder', commandCheckFolder),
    vscode.commands.registerCommand('otype.reloadRegistry', () => loadRegistry(true)),
    vscode.languages.registerCompletionItemProvider(selector, { provideCompletionItems: provideCompletion }, '.'),
    vscode.languages.registerHoverProvider(selector, { provideHover }),
    vscode.languages.registerSignatureHelpProvider(
      selector, { provideSignatureHelp },
      { triggerCharacters: [' ', '|'], retriggerCharacters: [' '] }),
    vscode.languages.registerDefinitionProvider(selector, { provideDefinition }),
    vscode.languages.registerDocumentSymbolProvider(selector, { provideDocumentSymbols: provideSymbols }),
    vscode.workspace.onDidOpenTextDocument((d) => scheduleCheck(d, true)),
    vscode.workspace.onDidChangeTextDocument((e) => {
      if (e.contentChanges.length === 0) return;
      if (cfg().get('liveCheck')) scheduleCheck(e.document, false);
    }),
    vscode.workspace.onDidSaveTextDocument((d) => scheduleCheck(d, true)),
    vscode.window.onDidChangeActiveTextEditor(() => updateStatus()),
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration('otype.pythonPath') || e.affectsConfiguration('otype.checkerPath')) {
        warned = false;
        loadRegistry(false).then(() => {
          for (const d of vscode.workspace.textDocuments) scheduleCheck(d, true);
        });
      }
    }),
    { dispose: () => { for (const r of running.values()) { r.cancelled = true; try { r.child.kill(); } catch (e) { /* gone */ } } } },
  );

  loadRegistry(false).then(() => {
    for (const d of vscode.workspace.textDocuments) scheduleCheck(d, true);
    updateStatus();
  });
}

function deactivate() {
  for (const r of running.values()) {
    r.cancelled = true;
    try { r.child.kill(); } catch (e) { /* gone */ }
  }
  running.clear();
  for (const t of timers.values()) clearTimeout(t);
  timers.clear();
}

module.exports = { activate, deactivate };
