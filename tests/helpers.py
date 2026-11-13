"""Shared sample data for the C fixture (sat.h): requirements, tests, implementations, audit outputs."""

from __future__ import annotations

from autospec.core.schemas import (
    AuditLLMOutput,
    Requirement,
    RequirementAuditItem,
    RequirementList,
)

REQS = RequirementList(
    requirements=[
        Requirement(req_id="REQ-001", title="範囲内の加算", description="和を返す",
                    acceptance_criteria=["sat_add16(100,23)=123"], priority="must"),
        Requirement(req_id="REQ-002", title="飽和", description="範囲外は飽和",
                    acceptance_criteria=["sat_add16(32000,1000)=32767"], priority="must"),
        Requirement(req_id="REQ-003", title="clamp", description="[lo,hi] に制限",
                    acceptance_criteria=["clamp_int(5,0,3)=3"], priority="must"),
    ],
    assumptions=[], open_questions=[],
)

GOOD_TEST = r'''#include "sat.h"
#include "autospec_test.h"

static void test_REQ001_add_in_range(void) {
    TEST_BEGIN("test_REQ001_add_in_range");
    CHECK_EQ_INT(sat_add16(100, 23), 123);
    CHECK_EQ_INT(sat_add16(-5, 3), -2);
}

static void test_REQ002_add_saturates(void) {
    TEST_BEGIN("test_REQ002_add_saturates");
    CHECK_EQ_INT(sat_add16(32000, 1000), 32767);
    CHECK_EQ_INT(sat_add16(-32000, -1000), -32768);
}

static void test_REQ003_clamp(void) {
    TEST_BEGIN("test_REQ003_clamp");
    CHECK_EQ_INT(clamp_int(5, 0, 3), 3);
    CHECK_EQ_INT(clamp_int(-1, 0, 3), 0);
    CHECK_EQ_INT(clamp_int(2, 0, 3), 2);
}

int main(void) {
    test_REQ001_add_in_range();
    test_REQ002_add_saturates();
    test_REQ003_clamp();
    return AUTOSPEC_SUMMARY();
}
'''

# REQ-002 missing (coverage_check must send this back to test_gen)
TEST_MISSING_REQ002 = r'''#include "sat.h"
#include "autospec_test.h"

static void test_REQ001_add_in_range(void) {
    TEST_BEGIN("test_REQ001_add_in_range");
    CHECK_EQ_INT(sat_add16(100, 23), 123);
}

static void test_REQ003_clamp(void) {
    TEST_BEGIN("test_REQ003_clamp");
    CHECK_EQ_INT(clamp_int(5, 0, 3), 3);
}

int main(void) {
    test_REQ001_add_in_range();
    test_REQ003_clamp();
    return AUTOSPEC_SUMMARY();
}
'''

# Checks nothing: passes even with a stub (red_check must catch it)
ALWAYS_PASS_TEST = r'''#include "sat.h"
#include "autospec_test.h"

static void test_REQ001_add_in_range(void) { TEST_BEGIN("test_REQ001_add_in_range"); CHECK(1); }
static void test_REQ002_add_saturates(void) { TEST_BEGIN("test_REQ002_add_saturates"); CHECK(1); }
static void test_REQ003_clamp(void) { TEST_BEGIN("test_REQ003_clamp"); CHECK(1); }

int main(void) {
    test_REQ001_add_in_range();
    test_REQ002_add_saturates();
    test_REQ003_clamp();
    return AUTOSPEC_SUMMARY();
}
'''

STUB = r'''#include "sat.h"

int16_t sat_add16(int16_t a, int16_t b) { (void)a; (void)b; return 0; }
int clamp_int(int v, int lo, int hi) { (void)v; (void)lo; (void)hi; return 0; }
'''

GOOD_IMPL = r'''#include "sat.h"

int16_t sat_add16(int16_t a, int16_t b) {
    int32_t s = (int32_t)a + (int32_t)b;
    if (s > INT16_MAX) return INT16_MAX;
    if (s < INT16_MIN) return INT16_MIN;
    return (int16_t)s;
}

int clamp_int(int v, int lo, int hi) {
    return v < lo ? lo : (v > hi ? hi : v);
}
'''

ONE_FAIL_IMPL = r'''#include "sat.h"

int16_t sat_add16(int16_t a, int16_t b) {
    return (int16_t)(a + b);
}

int clamp_int(int v, int lo, int hi) {
    return v < lo ? lo : (v > hi ? hi : v);
}
'''

TWO_FAIL_IMPL = r'''#include "sat.h"

int16_t sat_add16(int16_t a, int16_t b) {
    return (int16_t)(a + b);
}

int clamp_int(int v, int lo, int hi) {
    (void)lo; (void)hi;
    return v;
}
'''

BROKEN_IMPL = r'''#include "sat.h"

int16_t sat_add16(int16_t a, int16_t b) {
    return a + ;
}
'''


def fenced(code: str, tag: str = "c", before: str = "以下がテストです。", after: str = "") -> str:
    return f"{before}\n\n```{tag}\n{code}```\n{after}"


def audit_ok() -> AuditLLMOutput:
    return AuditLLMOutput(
        requirements=[
            RequirementAuditItem(req_id="REQ-001", description="和", status="PASS", implemented_in=["sat_add16"],
                                 verified_by=["test_REQ001_add_in_range"], note="ok"),
            RequirementAuditItem(req_id="REQ-002", description="飽和", status="PASS", implemented_in=["sat_add16()"],
                                 verified_by=["test_REQ002_add_saturates"], note="ok | 境界値も確認\n2行目"),
            RequirementAuditItem(req_id="REQ-003", description="clamp", status="PASS", implemented_in=["sat.c:clamp_int"],
                                 verified_by=["test_REQ003_clamp"], note="ok"),
        ],
        potential_risks=[], summary="全要件を満たす",
    )


def audit_with_fake_name() -> AuditLLMOutput:
    out = audit_ok()
    out.requirements[2].implemented_in = ["clamp_value_fast"]
    return out
