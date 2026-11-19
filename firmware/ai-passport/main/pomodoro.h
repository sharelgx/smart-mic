#pragma once

#include <stdbool.h>
#include <stdint.h>

typedef struct {
    bool resting;
    bool finished;
    int64_t remaining_us;
    int64_t deadline_us;
} pomodoro_t;

static inline int64_t pomodoro_duration(const pomodoro_t *timer) {
    return (timer->resting ? 5LL : 25LL) * 60 * 1000000;
}

static inline void pomodoro_reset(pomodoro_t *timer) {
    timer->remaining_us = pomodoro_duration(timer);
    timer->deadline_us = 0;
    timer->finished = false;
}

static inline void pomodoro_update(pomodoro_t *timer, int64_t now) {
    if (!timer->deadline_us) return;
    timer->remaining_us = timer->deadline_us - now;
    if (timer->remaining_us <= 0) {
        timer->remaining_us = 0;
        timer->deadline_us = 0;
        timer->finished = true;
    }
}

static inline void pomodoro_toggle(pomodoro_t *timer, int64_t now) {
    pomodoro_update(timer, now);
    if (timer->deadline_us) {
        timer->deadline_us = 0;
        return;
    }
    if (timer->finished) {
        timer->resting = !timer->resting;
        pomodoro_reset(timer);
    }
    timer->deadline_us = now + timer->remaining_us;
}
