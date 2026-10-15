# OType for VS Code

Editor support for OType, scripting for the Octatrack (spec: `docs/syntax.md`).
The language is case-insensitive (`tr.add`, `TR.ADD`, `Tr.Add` are the same); its canonical
written form is **lower case**, and that is what the editor
completes, shows and reports. Strings keep their case. The grammar still colours any casing.
Plain JavaScript, no dependencies, no build step. The language itself is not in the
extension: the checker (`tools/otype.py`, a copy is bundled in `checker/`) is run as a
child process, so the editor always agrees with `python3 tools/otype.py check`.

## What it does

- **Syntax highlighting**, brackets, `#` comments, indentation (a line ending with `:`
  indents the next one, `elif`/`else` dedent), snippets (`def`, `brick`, `page`, `loop`, `if`,
  `ifelse`, `each`, `param`, `const`, `prob`). `page "LABEL"` (a function on a page of
  encoders) is coloured, completed and hovered like the other declarations.
- **Live checking.** On open, on change (debounced) and on save, the whole folder of the
  document is checked as one program (functions and `const` are global across files); the
  text of the document comes from the editor buffer (`--stdin-file`), the other `.OTY`
  files from disk. Errors and warnings land in the Problems panel for every file of the
  folder. Status bar: `OType ✓ 8` (functions OK) or `OType 2 errors` (click: Problems).
  A run still going is killed when a newer one starts. If Python or the checker cannot run,
  one warning is shown and nothing else breaks.
- **Completion**: API operations (after `.` too: `tr.` lists `tr.*`), keywords, variables,
  your functions (`name | ` is inserted when it takes arguments), consts, and the arguments
  and params of the function the cursor is in. Everything is inserted in lower case
  (`tr.add`, `def`, `menu`), whatever you typed.
- **Hover**: operations (signature, doc, category, aliases, destructive), your functions
  (`def name | args`, menu path, cost, file:line), consts (value), arguments, params.
  Hovering `TR.ADD` or `Tr.Add` shows `tr.add s`.
- **Signature help** (typing a space or `|`): prefix notation is followed token by token,
  so `pl.set i amp.vol rrand 70 ` highlights the second parameter of `rrand a b`. The logic
  is in `lib/sighelp.js` (no `vscode` import).
- **Go to definition** of your functions and consts (any file of the folder), **outline**
  (functions with their menu path, consts).
- **Commands**: `OType: Check folder` (runs `check -v`, output in the "OType" channel),
  `OType: Reload registry` (after you edit `modules/otype/ops.py`, if `otype.checkerPath`
  points at the repo).

## Install for development

Nothing is installed by the repo. Recent VS Code versions ignore a folder (or a
symlink) dropped into `~/.vscode/extensions/`: only installed extensions are
loaded (seen on VS Code 1.140). Build a `.vsix` (a zip, no npm needed) and
install it:

```sh
python3 editors/vscode/package_vsix.py      # -> editors/vscode/otype-0.1.0.vsix
code --install-extension editors/vscode/otype-0.1.0.vsix --force
```

(`code` is `/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code`
if the shell command is not installed; or use **Extensions: Install from
VSIX…** in VS Code.) Then run **Developer: Reload Window** and open any `.OTY`
file. The install is a copy: rebuild and reinstall after changing the
extension. To try it without installing: open `editors/vscode` in VS Code and
press F5, an Extension Development Host starts.

Requirement: a Python 3 interpreter (`python3` by default).

## Settings

| setting | default | meaning |
|---|---|---|
| `otype.pythonPath` | `python3` | interpreter used to run the checker |
| `otype.checkerPath` | `` | path of `tools/otype.py` to use instead of the bundled copy (empty = bundled) |
| `otype.liveCheck` | `true` | check while typing; off = on open and on save only |
| `otype.debounceMs` | `300` | delay after the last keystroke before checking |

To follow the repo without refreshing the copy, set `otype.checkerPath` to
`/path/to/otype/tools/otype.py` (it finds its `modules/otype/ops.py` by itself).

## Refreshing the bundled checker

`checker/` is a copy of `tools/otype.py` and `modules/otype/` with the repo's layout. After a
change to the language, the registry or the checker:

```sh
editors/vscode/sync_checker.sh
```

then **OType: Reload registry** (or reload the window). The unit tests of the checker are
`python3 -m unittest tools/test_otype.py`; the signature help tests, which read the registry
from the bundled copy, are `node editors/vscode/test/sighelp.test.js`.

## Packaging and installing

`package_vsix.py` builds the extension without Node or `vsce` (a `.vsix` is a zip). Install it in VS Code with
"Extensions: Install from VSIX…", or from a terminal:

```sh
cd editors/vscode && python3 package_vsix.py       # produces otype-0.1.0.vsix
code --install-extension otype-0.1.0.vsix
```

## Not verified without VS Code running

The checker, the registry reading, the diagnostic mapping and the signature logic are tested
(Python unit tests, node tests, and the provider functions run against a stand-in `vscode`
module). What only a real VS Code can show: the look of the highlighting under a theme, the
auto-indent after `:` (`onEnterRules`), the word range of dotted names and `1:4`, and the
timing of signature help triggers.
