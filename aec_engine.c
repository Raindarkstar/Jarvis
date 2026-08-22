
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>

#define MAX_DELAY 4800
#define FILTER_LEN 512
#define MU 0.15f
#define EPS 1e-4f

typedef struct {
    float w[FILTER_LEN];
    float x_ring[MAX_DELAY + FILTER_LEN];
    int ring_idx;
    int est_delay;
    float alpha;
    float power_x;
    float power_d;
    float power_e;
} AECState;

AECState* aec_create() {
    AECState* st = (AECState*)calloc(1, sizeof(AECState));
    st->ring_idx = 0;
    st->est_delay = 320; // default ~20ms delay
    st->alpha = 0.95f;
    return st;
}

void aec_destroy(AECState* st) {
    if (st) free(st);
}

void aec_set_delay(AECState* st, int delay_samples) {
    if (delay_samples >= 0 && delay_samples < MAX_DELAY) {
        st->est_delay = delay_samples;
    }
}

// Process 16-bit integer PCM chunks (e.g. 160 samples = 10ms at 16kHz)
void aec_process(AECState* st, const short* mic_in, const short* ref_in, short* clean_out, int num_samples) {
    for (int n = 0; n < num_samples; n++) {
        float x_val = (float)ref_in[n] / 32768.0f;
        float d_val = (float)mic_in[n] / 32768.0f;

        // Push into reference ring buffer
        st->x_ring[st->ring_idx] = x_val;
        
        // Calculate echo estimate
        float y_hat = 0.0f;
        float x_energy = 0.0f;
        
        for (int k = 0; k < FILTER_LEN; k++) {
            int idx = st->ring_idx - st->est_delay - k;
            if (idx < 0) idx += (MAX_DELAY + FILTER_LEN);
            float x_k = st->x_ring[idx];
            y_hat += st->w[k] * x_k;
            x_energy += x_k * x_k;
        }

        // Advance ring index
        st->ring_idx = (st->ring_idx + 1) % (MAX_DELAY + FILTER_LEN);

        // Error signal
        float e = d_val - y_hat;

        // Power tracking
        st->power_x = st->alpha * st->power_x + (1.0f - st->alpha) * (x_val * x_val);
        st->power_d = st->alpha * st->power_d + (1.0f - st->alpha) * (d_val * d_val);
        st->power_e = st->alpha * st->power_e + (1.0f - st->alpha) * (e * e);

        // Double talk detection: only update weights when near-end is not dominant
        if (x_energy > EPS) {
            float norm = MU / (x_energy + EPS);
            // If near-end speech power is huge compared to echo, freeze adaptation
            if (st->power_d < st->power_x * 4.0f + 0.01f) {
                for (int k = 0; k < FILTER_LEN; k++) {
                    int idx = (st->ring_idx - 1) - st->est_delay - k;
                    if (idx < 0) idx += (MAX_DELAY + FILTER_LEN);
                    st->w[k] += norm * e * st->x_ring[idx];
                }
            }
        }

        // Convert back to short with clipping
        int out_int = (int)(e * 32768.0f);
        if (out_int > 32767) out_int = 32767;
        if (out_int < -32768) out_int = -32768;
        clean_out[n] = (short)out_int;
    }
}
