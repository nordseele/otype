# OType Audio — Host Imports Contract

This document specifies the C host imports contract for OType's audio operators.
It specifies the C ABI, function signatures, error codes, limits, execution requirements,
and the `input sample` placement header bit.

---

## 1. Generic C ABI

The host side implements 9 functions declared in `firmware/c/otc_vm.h`:

- C ABI: standard `stdint.h` types, arguments passed by value / stack, results returned as `int32_t` or `uint32_t`.
- Sample slot indices: **0..127** in C (the OType language uses user-facing 1..128 and maps to 0..127).
- File paths: canonical UTF-8, absolute relative to the root of the host storage, without `..`.
- Path buffers: **392 bytes** maximum, including terminating NUL.
- Validation: the host validates all paths before performing any I/O.
- Pointers: no pointer borrowed from the VM is retained after the host function returns.
- Concurrency: exactly one script execution and one audio host operation can be active at a time.

```c
uint32_t otb_host_audio_input(void); /* 0 = none, 1 = sample */
int32_t otb_host_src_save(char *out, uint32_t cap);
int32_t otb_host_flex_load(uint32_t slot0, const char *path);
int32_t otb_host_flex_free(uint32_t slot0);
int32_t otb_host_flex_del(uint32_t slot0);
void    otb_host_cat_clr(void);
int32_t otb_host_cat_add(const char *path);
int32_t otb_host_cat_save(const char *name, char *out, uint32_t cap);
int32_t otb_host_norm(const char *path, int32_t db);
```

### Return Values

- Most operations return **0 on success**, or a negative error code (see §2 below).
- `otb_host_flex_free` returns:
  - `1`: slot was empty / freed
  - `0`: slot was occupied and cleared
  - `< 0`: negative error code
- `otb_host_flex_del` returns `0` (slot emptied, or already empty and left untouched), `-1` (`OTC_AE_BAD_ARG`) for
  a slot above 127, `-6` (`OTC_AE_STOP_FIRST`) for an occupied slot while the sequencer plays.
- `otb_host_audio_input` returns `0` (no input sample context) or `1` (input sample context available). It does not perform I/O.
- `out` path buffers are only populated on success and never truncated. If buffer capacity `cap` is insufficient, `-1` (`OTC_AE_BAD_ARG`) is returned before writing.

---

## 2. Error Codes

Audio import error codes are negative integers `-1..-13`. The VM retains and displays the detailed audio error code:

| Code | Constant | VM Message | Description |
|---|---|---|---|
| -1 | `OTC_AE_BAD_ARG` | BAD ARG | Invalid slot, path, name, buffer, or dB value |
| -2 | `OTC_AE_CARD_BUSY` | CARD BUSY | Concurrent operation or mutex unavailable |
| -3 | `OTC_AE_NO_SOURCE` | NO SOURCE | Execution context lacks an input sample |
| -4 | `OTC_AE_EMPTY` | EMPTY | Source buffer empty, list empty, or WAV without audio frames |
| -5 | `OTC_AE_FLEX_FULL` | FLEX FULL | Flex RAM allocation refused by host |
| -6 | `OTC_AE_STOP_FIRST` | STOP FIRST | Recording active; or replacing or clearing a Flex sample while the sequencer plays |
| -7 | `OTC_AE_EXISTS` | EXISTS | Output WAV or associated sidecar already exists |
| -8 | `OTC_AE_FORMAT` | FORMAT | Unsupported PCM (not 44.1 kHz, mono/stereo, 16/24-bit) or malformed file |
| -9 | `OTC_AE_CARD_FULL` | CARD FULL | Storage full / disk write allocation failed |
| -10 | `OTC_AE_IO_ERROR` | IO ERROR | Read, write, seek, close, or filesystem I/O failure |
| -11 | `OTC_AE_TIMEOUT` | TIMEOUT | Operation exceeded maximum allowed duration |
| -12 | `OTC_AE_CAT_FULL` | CAT FULL | Concatenation list capacity reached (64 items) |
| -13 | `OTC_AE_TOO_LONG` | TOO LONG | WAV size exceeds host safety limit |

---

## 3. Operations & Semantics

### Concatenation List (`cat.clr`, `cat.add`, `cat.save`)
- The host maintains an internal list of up to **64 paths**.
- The VM automatically calls `cat.clr` **at the start of each root execution**. Nested function calls do NOT reset the list.
- `cat.add`: validates path and capacity (max 64 items), then appends. Duplicate paths are allowed.
- `cat.save`: takes a base name (1..63 ASCII characters: letters, numbers, space, `_`, `-`; no extension or slashes).
  - Target directory: context sample directory if available, else `/AUDIO`.
  - Output format: 44.1 kHz PCM, stereo if any source item is stereo (mono duplicated), 24-bit if any source item is 24-bit, else 16-bit.
  - Slices: writes one `.ot` slice per concatenated item (frame boundaries, loop -1).
  - Refuses to overwrite if the target `.wav` or `.ot` already exists (`-7`).
  - Size check: PCM byte limit of `0x7ffff000` checked before creation.
  - On failure, partially created files are removed.
  - Does not clear the list on completion; only `cat.clr` clears it.
  - On success, the current frame's `FILE` list is replaced by the single resulting WAV path (accessible via `file.at 1`).

### Saving the source take (`src.save`)
- Requires `input sample` placement header on the calling function (and its callers).
- Checks host source context; saves durable WAV.
- On success, the current frame's `FILE` list is replaced with the newly saved WAV path (`file.at 1`).

### Flex Slots (`flex.load`, `flex.free`, `flex.firstfree`, `flex.del`)
- `flex.load slot path`: loads WAV from `path` into Flex slot `slot` (1..128 in script, 0..127 in C). Marks slot as touched for UI update.
- `flex.free slot`: tests Flex slot `slot` (it frees nothing). Returns `1` if empty, `0` if occupied.
- `flex.firstfree`: number of the first empty Flex slot (1..128), `0` if none. Language only: the VM calls
  `otb_host_flex_free` for slots 0..127 in order, like `static.firstfree`. A host error stops the run.
- `flex.del slot`: empties Flex slot `slot` (the Flex twin of `static.clr`; the verb is `del` after the naming rules).
  The VM asks `otb_host_flex_free` first: an empty slot is not touched (no host call, no UI refresh).
  For an occupied slot the host runs the device's own slot clear, so that the sample's RAM goes back to the Flex pool and
  nothing stays open; it never clears a record by hand. The host refuses an occupied slot with `-6` while the sequencer
  plays (as `flex.load` does when it replaces a sample): freeing the RAM can move the samples of the later slots.
  Afterwards `flex.free slot` returns `1`. The slots-changed flag is set so the host refreshes the sample views.

### Normalization (`norm`)
- `norm path db`: target peak level `db` (-12..0 dB).
- Scans all frames/channels of `path`, computes required gain rounded to 0.5 dB steps within ±24 dB.
- Updates or creates associated `.ot` metadata with correct checksum.

---

## 4. UI Feedback and Timeouts

- The host displays `WORKING...` before long-running operations.
- Long operations sleep in **10 ms** increments (never busy-wait).
- Timeouts:
  - `src.save`: 120 seconds
  - `flex.load`: 120 seconds
  - `cat.save`: 600 seconds
  - `norm`: 600 seconds
  - `cat.add`, `cat.clr`, `flex.free`, `flex.del`, `audio_input`: immediate (no storage job, no card access; `flex.del` zeroes the freed Flex RAM in the calling task)
- On timeout, the host cancels the background job, waits for cleanup, and returns `-11` (`OTC_AE_TIMEOUT`).

---

## 5. Bytecode Function Header: `input sample`

The OTB function entry remains **54 bytes**.
- Bit `0x10` (`OTC_FL_INPUT_SAMPLE`) of entry byte `+23` (flags) indicates `input sample`:
  - `0x00`: normal function (no sample input required).
  - `0x10`: function expects an input sample context from the host menu.
- The language compiler (`tools/otype.py` / `otc_comp`) validates at compile time that:
  - Any function using `src.*` declares `input sample`.
  - Any caller of an `input sample` function also declares `input sample`.
- At runtime, the host menu uses `otb_host_audio_input()` to determine whether `input sample` actions should be displayed or executed.
