#include "encoders.h"

#include "config.h"
#include "hardware/pio.h"
#include "quadrature_encoder.pio.h"

namespace encoders {
namespace {

PIO const g_pio = pio0;
int g_sm[2] = {-1, -1};

}  // namespace

bool init() {
    // The program uses computed jumps and must live at offset 0.
    if (!pio_can_add_program_at_offset(g_pio, &quadrature_encoder_program, 0)) return false;
    pio_add_program_at_offset(g_pio, &quadrature_encoder_program, 0);
    g_sm[0] = pio_claim_unused_sm(g_pio, true);
    g_sm[1] = pio_claim_unused_sm(g_pio, true);
    // max_step_rate = 0: sample at full sys clock (plenty of margin).
    quadrature_encoder_program_init(g_pio, g_sm[0], cfg::PIN_ENC_L_A, 0);
    quadrature_encoder_program_init(g_pio, g_sm[1], cfg::PIN_ENC_R_A, 0);
    return true;
}

int32_t count_left() {
    const int32_t c = quadrature_encoder_get_count(g_pio, g_sm[0]);
    return cfg::ENC_L_INVERT ? -c : c;
}

int32_t count_right() {
    const int32_t c = quadrature_encoder_get_count(g_pio, g_sm[1]);
    return cfg::ENC_R_INVERT ? -c : c;
}

}  // namespace encoders
