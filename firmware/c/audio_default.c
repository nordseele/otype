/* Default audio imports, for an integration that does not implement them (docs/audio-imports-contract.md).
 * build/cunits.py compiles them into the VM unit with -DOTYPE_AUDIO_DEFAULTS, as weak symbols: a firmware that
 * implements the nine imports links its own definitions and they win. With these defaults no take is ever available
 * and every audio operator stops the run with IO ERROR. */
#include "otc_vm.h"

#define OTC_WEAK __attribute__((weak))

OTC_WEAK uint32_t otb_host_audio_input(void) { return 0; }
OTC_WEAK int32_t otb_host_src_save(char *out, uint32_t cap) { (void)out; (void)cap; return OTC_AE_IO_ERROR; }
OTC_WEAK int32_t otb_host_flex_load(uint32_t slot0, const char *path) { (void)slot0; (void)path; return OTC_AE_IO_ERROR; }
OTC_WEAK int32_t otb_host_flex_free(uint32_t slot0) { (void)slot0; return OTC_AE_IO_ERROR; }
OTC_WEAK int32_t otb_host_flex_del(uint32_t slot0) { (void)slot0; return OTC_AE_IO_ERROR; }
OTC_WEAK void otb_host_cat_clr(void) { }
OTC_WEAK int32_t otb_host_cat_add(const char *path) { (void)path; return OTC_AE_IO_ERROR; }
OTC_WEAK int32_t otb_host_cat_save(const char *name, char *out, uint32_t cap) { (void)name; (void)out; (void)cap; return OTC_AE_IO_ERROR; }
OTC_WEAK int32_t otb_host_norm(const char *path, int32_t db) { (void)path; (void)db; return OTC_AE_IO_ERROR; }
