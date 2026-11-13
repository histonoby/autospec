/*
 * autospec_test.h - tiny test helper for AutoSpec generated C/C++ tests.
 * Failures are counted (never abort), every failure prints
 *   FAIL <test name> <file>:<line> <expr>
 * and AUTOSPEC_SUMMARY() prints "PASSED: n FAILED: m" and returns non-zero on failure.
 * Deliberately independent of assert.h (first-failure abort / -DNDEBUG pass-through).
 */
#ifndef AUTOSPEC_TEST_H
#define AUTOSPEC_TEST_H

#include <math.h>
#include <stdio.h>

static int autospec_passed_ = 0;
static int autospec_failed_ = 0;
static int autospec_in_test_ = 0;
static int autospec_cur_failed_ = 0;
static const char *autospec_current_ = "(global)";

static inline void autospec_end_(void) {
    if (autospec_in_test_) {
        if (autospec_cur_failed_) autospec_failed_++;
        else autospec_passed_++;
        autospec_in_test_ = 0;
    }
}

static inline void autospec_begin_(const char *name) {
    autospec_end_();
    autospec_current_ = name;
    autospec_in_test_ = 1;
    autospec_cur_failed_ = 0;
}

static inline void autospec_fail_(const char *file, int line, const char *expr) {
    if (!autospec_in_test_) autospec_begin_("(global)");
    autospec_cur_failed_++;
    printf("FAIL %s %s:%d %s\n", autospec_current_, file, line, expr);
    fflush(stdout);
}

static inline int autospec_summary_(void) {
    autospec_end_();
    if (autospec_passed_ + autospec_failed_ == 0) {
        printf("FAIL (no-tests) no TEST_BEGIN was executed\n");
        autospec_failed_ = 1;
    }
    printf("PASSED: %d FAILED: %d\n", autospec_passed_, autospec_failed_);
    fflush(stdout);
    return autospec_failed_ ? 1 : 0;
}

#define TEST_BEGIN(name) autospec_begin_((name))

#define CHECK(cond)                                                    \
    do {                                                               \
        if (!(cond)) autospec_fail_(__FILE__, __LINE__, #cond);        \
    } while (0)

#define CHECK_EQ_INT(actual, expected)                                                   \
    do {                                                                                 \
        long long autospec_a_ = (long long)(actual);                                     \
        long long autospec_e_ = (long long)(expected);                                   \
        if (autospec_a_ != autospec_e_) {                                                \
            char autospec_msg_[256];                                                     \
            snprintf(autospec_msg_, sizeof autospec_msg_, "%s == %s (actual=%lld expected=%lld)", \
                     #actual, #expected, autospec_a_, autospec_e_);                      \
            autospec_fail_(__FILE__, __LINE__, autospec_msg_);                           \
        }                                                                                \
    } while (0)

#define CHECK_NEAR(actual, expected, eps)                                                \
    do {                                                                                 \
        double autospec_a_ = (double)(actual);                                           \
        double autospec_e_ = (double)(expected);                                         \
        if (!(fabs(autospec_a_ - autospec_e_) <= (double)(eps))) {                       \
            char autospec_msg_[256];                                                     \
            snprintf(autospec_msg_, sizeof autospec_msg_, "%s ~= %s (actual=%.9g expected=%.9g eps=%g)", \
                     #actual, #expected, autospec_a_, autospec_e_, (double)(eps));       \
            autospec_fail_(__FILE__, __LINE__, autospec_msg_);                           \
        }                                                                                \
    } while (0)

#define AUTOSPEC_SUMMARY() autospec_summary_()

#endif /* AUTOSPEC_TEST_H */
