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
OTC_WEAK void otb_host_module_play(uint32_t track0, const uint8_t *locks32) { (void)track0; (void)locks32; }
OTC_WEAK uint32_t otb_host_seq_run(void) { return 0; }
OTC_WEAK uint32_t otb_host_seq_step(uint32_t track0) { (void)track0; return 1; }
OTC_WEAK uint32_t otb_host_seq_mute(uint32_t track0) { (void)track0; return 0; }
OTC_WEAK void otb_host_send_note(uint32_t ch, uint32_t note, uint32_t vel, uint32_t dur) { (void)ch; (void)note; (void)vel; (void)dur; }
OTC_WEAK void otb_host_send_cc(uint32_t ch, uint32_t num, uint32_t val) { (void)ch; (void)num; (void)val; }
OTC_WEAK void otb_host_send_prog(uint32_t ch, uint32_t prog) { (void)ch; (void)prog; }
OTC_WEAK void otb_host_send_nrpn(uint32_t ch, uint32_t par, uint32_t val) { (void)ch; (void)par; (void)val; }
OTC_WEAK uint32_t otb_host_midi_cc(uint32_t ch, uint32_t num) { (void)ch; (void)num; return 0; }
OTC_WEAK uint32_t otb_host_midi_note(uint32_t ch) { (void)ch; return 0; }
OTC_WEAK uint32_t otb_host_midi_vel(uint32_t ch) { (void)ch; return 0; }
