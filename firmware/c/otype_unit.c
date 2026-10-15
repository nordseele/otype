/* The ONE translation unit of OType's C for the ColdFire DRAM unit (and for the Mac library).
 * build/cunits.py compiles exactly this file. */
#include "probe.c"      /* the PRNG proof, otc_probe */
#include "vm.c"         /* the bytecode VM */
#ifdef __m68k__
#include "machine.c"
#endif
#ifdef OTYPE_AUDIO_DEFAULTS
#include "audio_default.c"   /* weak audio imports for a firmware without audio (build/cunits.py) */
#endif
