/* Host (Mac) stand-ins for the asm-side symbols of the C unit: lets the same probe.c / vm.c be built as
 * a shared library and compared against the Python reference and the Unicorn run.
 *
 * the card / slot imports (otb_host_dir_open ... otb_host_card_busy, declared in otc_vm.h) call
 * function pointers that Python sets with otb_shim_set (ctypes CFUNCTYPE, modules/otype/cvm.py: HostCard,
 * HostSlots). Not set (NULL): an empty card (-1 absent), every slot free, nothing loads, never busy.
 * otb_host_param_info / otb_host_param_value call two more pointers (otb_shim_set_params): the part's
 * descriptor and value as modules/otype/cvm.py's HostParams serves them.  Not set (NULL): the neutral model
 * (range 0..127, enabled, value 0).
 * the nine part imports (otb_host_par_set ... otb_host_trk_slot_init) likewise (otb_shim_set_part, at the end).
 * This file is host-only, so its writable statics are allowed here (the unit never contains it). */
#include <stdint.h>
#include "otc_vm.h"
#include "otc_ops.h"

uint32_t otb_host_add(uint32_t a, uint32_t b)
{
    return a + b;
}

typedef int32_t (*hk_open)(void *, const char *, uint32_t);
typedef int32_t (*hk_next)(void *, char *);
typedef uint32_t (*hk_skipped)(void *);
typedef int32_t (*hk_name)(uint32_t, char *, uint32_t);
typedef int32_t (*hk_set)(uint32_t, const char *);
typedef int32_t (*hk_load)(uint32_t);
typedef void (*hk_clr)(uint32_t);
typedef int32_t (*hk_busy)(uint32_t);

static hk_open h_open;
static hk_next h_next;
static hk_skipped h_skipped;
static hk_name h_name;
static hk_set h_set;
static hk_load h_load;
static hk_clr h_clr;
static hk_busy h_busy;

void otb_shim_set(void *open, void *next, void *skipped, void *name, void *set, void *load, void *clr,
                  void *busy)
{
    h_open = (hk_open)open;
    h_next = (hk_next)next;
    h_skipped = (hk_skipped)skipped;
    h_name = (hk_name)name;
    h_set = (hk_set)set;
    h_load = (hk_load)load;
    h_clr = (hk_clr)clr;
    h_busy = (hk_busy)busy;
}

int32_t otb_host_dir_open(void *it, const char *canon, uint32_t deep)
{
    return h_open ? h_open(it, canon, deep) : -1;
}

int32_t otb_host_dir_next(void *it, char *out260)
{
    return h_next ? h_next(it, out260) : 0;
}

uint32_t otb_host_dir_skipped(void *it)
{
    return h_skipped ? h_skipped(it) : 0;
}

int32_t otb_host_slot_name(uint32_t slot0, char *out260, uint32_t max)
{
    return h_name ? h_name(slot0, out260, max) : 0;
}

int32_t otb_host_slot_set(uint32_t slot0, const char *path)
{
    return h_set ? h_set(slot0, path) : 1;
}

int32_t otb_host_slot_load(uint32_t slot0)
{
    return h_load ? h_load(slot0) : 0;
}

void otb_host_slot_clr(uint32_t slot0)
{
    if (h_clr)
        h_clr(slot0);
}

int32_t otb_host_card_busy(uint32_t op)
{
    return h_busy ? h_busy(op) : 0;
}

typedef void (*hk_pinfo)(uint32_t, uint32_t, uint32_t *, uint32_t *, uint32_t *);
typedef int32_t (*hk_pvalue)(uint32_t, uint32_t);
static hk_pinfo h_pinfo;
static hk_pvalue h_pvalue;

void otb_shim_set_params(void *info, void *value)
{
    h_pinfo = (hk_pinfo)info;
    h_pvalue = (hk_pvalue)value;
}

void otb_host_param_info(uint32_t track0, uint32_t p, uint32_t *lo, uint32_t *count, uint32_t *enabled)
{
    if (h_pinfo) {
        h_pinfo(track0, p, lo, count, enabled);
    } else {
        *lo = 0;
        *count = 128;
        *enabled = 1;
    }
}

int32_t otb_host_param_value(uint32_t track0, uint32_t p)
{
    return h_pvalue ? h_pvalue(track0, p) : 0;
}

/* the part imports (otc_vm.h) call nine more pointers (otb_shim_set_part), served by cvm.py's HostParams (the part's
 * host twin).  Not set (NULL): writes do nothing, the default is 0, no waveform, empty choosers (fx.* do nothing). */
typedef void (*hk_parset)(uint32_t, uint32_t, int32_t);
typedef int32_t (*hk_pdef)(uint32_t, uint32_t);
typedef void (*hk_lfoset)(uint32_t, uint32_t, uint32_t, uint32_t);
typedef uint32_t (*hk_waves)(void);
typedef uint32_t (*hk_fxn)(uint32_t);
typedef void (*hk_fxset)(uint32_t, uint32_t, uint32_t);
typedef void (*hk_scene)(uint32_t, uint32_t, uint32_t, uint32_t);
typedef void (*hk_slotinit)(uint32_t);
typedef void (*hk_trkslot)(uint32_t, uint32_t);
typedef void (*hk_tempo)(int32_t);
static hk_parset h_parset;
static hk_pdef h_pdef;
static hk_lfoset h_lfoset;
static hk_waves h_waves;
static hk_fxn h_fxcount, h_fxdef;
static hk_fxset h_fxset;
static hk_scene h_scene;
static hk_slotinit h_slotinit;
static hk_trkslot h_trkslot;
static hk_tempo h_tempo;

void otb_shim_set_part(void *parset, void *pdef, void *lfoset, void *waves, void *fxcount, void *fxdef, void *fxset,
                       void *scene, void *slotinit, void *trkslot, void *tempo)
{
    h_parset = (hk_parset)parset;
    h_pdef = (hk_pdef)pdef;
    h_lfoset = (hk_lfoset)lfoset;
    h_waves = (hk_waves)waves;
    h_fxcount = (hk_fxn)fxcount;
    h_fxdef = (hk_fxn)fxdef;
    h_fxset = (hk_fxset)fxset;
    h_scene = (hk_scene)scene;
    h_slotinit = (hk_slotinit)slotinit;
    h_trkslot = (hk_trkslot)trkslot;
    h_tempo = (hk_tempo)tempo;
}

void otb_host_par_set(uint32_t track0, uint32_t p, int32_t v)
{
    if (h_parset)
        h_parset(track0, p, v);
}

int32_t otb_host_param_default(uint32_t track0, uint32_t p)
{
    return h_pdef ? h_pdef(track0, p) : 0;
}

void otb_host_lfo_set(uint32_t track0, uint32_t lfo0, uint32_t what, uint32_t v)
{
    if (h_lfoset)
        h_lfoset(track0, lfo0, what, v);
}

uint32_t otb_host_lfo_waves(void)
{
    return h_waves ? h_waves() : 0;
}

uint32_t otb_host_fx_count(uint32_t slot0)
{
    return h_fxcount ? h_fxcount(slot0) : 0;
}

uint32_t otb_host_fx_default(uint32_t slot0)
{
    return h_fxdef ? h_fxdef(slot0) : 0xFFFFFFFFu;
}

void otb_host_fx_set(uint32_t track0, uint32_t slot0, uint32_t index)
{
    if (h_fxset)
        h_fxset(track0, slot0, index);
}

void otb_host_scene_put(uint32_t track0, uint32_t scene0, uint32_t byte, uint32_t v)
{
    if (h_scene)
        h_scene(track0, scene0, byte, v);
}

void otb_host_trk_slot_init(uint32_t track0)
{
    if (h_slotinit)
        h_slotinit(track0);
}

void otb_host_trk_slot(uint32_t track0, uint32_t slot0)
{
    if (h_trkslot)
        h_trkslot(track0, slot0);
}

void otb_host_tempo(int32_t bpm)
{
    if (h_tempo)
        h_tempo(bpm);
}

typedef int32_t (*hk_cread)(uint32_t, uint32_t, uint32_t);
typedef int32_t (*hk_cwrite)(uint32_t, uint32_t, uint32_t, int32_t);
typedef int32_t (*hk_action)(void *, uint32_t, int32_t, int32_t);
typedef void (*hk_refresh)(void *);
static hk_cread h_cread;
static hk_cwrite h_cwrite;
static hk_action h_action;
static hk_refresh h_refresh;

void otb_shim_set_control(void *rd, void *wr, void *action, void *refresh)
{
    h_cread = (hk_cread)rd;
    h_cwrite = (hk_cwrite)wr;
    h_action = (hk_action)action;
    h_refresh = (hk_refresh)refresh;
}

int32_t otb_host_control_read(uint32_t h, uint32_t t, uint32_t n)
{
    return h_cread ? h_cread(h, t, n) : 0;
}

int32_t otb_host_control_write(uint32_t h, uint32_t t, uint32_t n, int32_t v)
{
    return h_cwrite ? h_cwrite(h, t, n, v) : 0;
}

int32_t otb_host_control_action(otc_vm_t *vm, uint32_t h, int32_t a, int32_t b)
{
    int32_t rc = h_action ? h_action(vm, h, a, b) : OTC_E_UNSUPPORTED;
    if (!rc && (h == OTC_H_PAT_GO || h == OTC_H_PART_GO || h == OTC_H_PART_RELOAD) && h_cread)
        vm->length = (uint8_t)h_cread(OTC_H_PAT_LEN, vm->host_track - 1, 0);
    return rc;
}

void otb_host_refresh(otc_vm_t *vm)
{
    if (h_refresh) h_refresh(vm);
}

void otb_host_commit(otc_vm_t *vm)
{
    vm->pending_trac = vm->pending_part = 0;
}

/* audio imports */
typedef uint32_t (*hk_ainput)(void);
typedef int32_t (*hk_asrcsave)(char *, uint32_t);
typedef int32_t (*hk_aflexload)(uint32_t, const char *);
typedef int32_t (*hk_aflexfree)(uint32_t);
typedef int32_t (*hk_aflexdel)(uint32_t);
typedef void    (*hk_acatclr)(void);
typedef int32_t (*hk_acatadd)(const char *);
typedef int32_t (*hk_acatsave)(const char *, char *, uint32_t);
typedef int32_t (*hk_anorm)(const char *, int32_t);

static hk_ainput    h_ainput;
static hk_asrcsave  h_asrcsave;
static hk_aflexload h_aflexload;
static hk_aflexfree h_aflexfree;
static hk_aflexdel  h_aflexdel;
static hk_acatclr   h_acatclr;
static hk_acatadd   h_acatadd;
static hk_acatsave  h_acatsave;
static hk_anorm     h_anorm;

void otb_shim_set_audio(void *ainput, void *srcsave, void *flexload, void *flexfree,
                        void *flexdel, void *catclr, void *catadd, void *catsave, void *norm)
{
    h_ainput = (hk_ainput)ainput;
    h_asrcsave = (hk_asrcsave)srcsave;
    h_aflexload = (hk_aflexload)flexload;
    h_aflexfree = (hk_aflexfree)flexfree;
    h_aflexdel = (hk_aflexdel)flexdel;
    h_acatclr = (hk_acatclr)catclr;
    h_acatadd = (hk_acatadd)catadd;
    h_acatsave = (hk_acatsave)catsave;
    h_anorm = (hk_anorm)norm;
}

uint32_t otb_host_audio_input(void)
{
    return h_ainput ? h_ainput() : 0;
}

int32_t otb_host_src_save(char *out, uint32_t cap)
{
    return h_asrcsave ? h_asrcsave(out, cap) : OTC_AE_NO_SOURCE;
}

int32_t otb_host_flex_load(uint32_t slot0, const char *path)
{
    return h_aflexload ? h_aflexload(slot0, path) : 0;
}

int32_t otb_host_flex_free(uint32_t slot0)
{
    return h_aflexfree ? h_aflexfree(slot0) : 1;
}

int32_t otb_host_flex_del(uint32_t slot0)
{
    return h_aflexdel ? h_aflexdel(slot0) : 0;
}

void otb_host_cat_clr(void)
{
    if (h_acatclr)
        h_acatclr();
}

int32_t otb_host_cat_add(const char *path)
{
    return h_acatadd ? h_acatadd(path) : 0;
}

int32_t otb_host_cat_save(const char *name, char *out, uint32_t cap)
{
    return h_acatsave ? h_acatsave(name, out, cap) : OTC_AE_EMPTY;
}

int32_t otb_host_norm(const char *path, int32_t db)
{
    return h_anorm ? h_anorm(path, db) : 0;
}

