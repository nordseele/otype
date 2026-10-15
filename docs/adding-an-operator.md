# Proposing and adding an operator

Propose an operation with the [New operator issue template](../.github/ISSUE_TEMPLATE/new-operator.md).
Describe its device behavior, arguments, refused cases, evidence and playback safety before suggesting code.
A proposal does not imply that it will be accepted or supported.

## Naming

- An object number is an argument: `lfo.spd n`. `fx1` and `fx2` keep the physical button names.
- Use device objects: `pat`, `part`, `trk`, `scn`, `lfo`, `amp`, `pb`, `fx1`, `fx2`, `static`, `file`.
  `static` is the Static sample list; a Flex list would take its own object, `flex`.
  Trig families are `tr`, `tl`, `tt`, `os`, `sw`, `sl`.
- Use `set/get/del` for one item, `add/del/flip` for a step mark, `clr` for the context,
  `rnd` for randomness, `len` for a count, `cur` for the current index and `go` for a transition.
- A single setting is both readable and writable: `x tempo` reads; `tempo 120` writes.
- Use `trk all` for all tracks. A track operation uses the context instead of taking a track argument.
- Parameter names and direct settings differ: `lfo.spd n` identifies a parameter for `par.set` or `pl.set`;
  `lfo.mult n v` writes a SETUP value directly.
- Preserve numeric operation IDs. A rename rejects the old spelling and suggests the replacement; do not reuse its ID.

## Establish the device behavior

Target a specific OS and hardware model. Prefer the stock writer to a private rewrite.
Identify parameter descriptors, SRAM copies, dirty flags, DSP updates, and the refresh the operation needs.
Check the task context and behavior during playback, including queued pattern changes.
A static reading, an emulator test and a hardware observation are different levels of evidence.

## Implement the complete path

1. Add the declaration to `modules/otype/ops.py`: category, argument kinds, result, cost, family,
   destructive behavior and evidence level. A new machine operation stays outside the machine profile until validated.
2. Implement its behavior in `modules/otype/sim.py`. Reuse the existing state and descriptor model.
   `modules/otype/vm.py` runs the same handlers; update OTB generation only if a new construct needs it.
3. Regenerate both C registry headers:

   ```sh
   python3 modules/otype/gen_ops_c.py
   python3 modules/otype/gen_ops_c.py --check
   ```

4. Add the C handler in `firmware/c/vm.c` and the stock binding in `firmware/c/machine.c` or
   the firmware assembly bridge. Keep getters free of writes. Update every caller and build import list that changes.
   The C is freestanding: no heap, writable globals, libc or 64-bit arithmetic.
5. Build with `python3 firmware/build/build.py`, then follow the integration contract in
   [firmware/README.md](../firmware/README.md). The firmware tree is generic; each integrating tool supplies its placement.

## Validate and document

Add a focused regression with bounds and refusal cases, including a test that fails on the previous implementation.
Compare the simulator, Python VM and C/ColdFire execution; compare resulting bytes and SRAM, not only return codes.
Exercise the operation through the intended UI or event path as well as its handler.
If storage is involved, test FAT16 and FAT32. Confirm the result on hardware before claiming that it works there.

Run the relevant `tools/test_otype*.py` suites, regenerate `tools/cheatsheet.py`, and run
`bash editors/vscode/sync_checker.sh`. Update the command documentation and add a small cookbook recipe.
Do not include an Elektron image, disassembly or copied firmware data in an issue or pull request.
