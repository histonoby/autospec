// autospec_tb.svh - helpers for AutoSpec generated SystemVerilog testbenches.
// Usage inside the tb module:
//   `AUTOSPEC_TB_DECLS
//   `AUTOSPEC_WATCHDOG(100000)
//   `CHECK_EQ(dut_out, 8'h2A, "test_REQ001_basic")
//   `AUTOSPEC_FINISH
// Output contract (compatible with the legacy judge): "PASSED: n FAILED: m", then PASS or "FATAL: mismatch";
// the watchdog prints "FATAL: timeout".
`ifndef AUTOSPEC_TB_SVH
`define AUTOSPEC_TB_SVH

`define AUTOSPEC_TB_DECLS \
  int autospec_passed = 0; \
  int autospec_failed = 0;

`define CHECK_EQ(actual, expected, name) \
  begin \
    if ((actual) !== (expected)) begin \
      autospec_failed = autospec_failed + 1; \
      $display("FAIL %s actual=%0h expected=%0h", name, (actual), (expected)); \
    end else begin \
      autospec_passed = autospec_passed + 1; \
    end \
  end

`define AUTOSPEC_WATCHDOG(limit) \
  initial begin \
    #(limit); \
    $display("FATAL: timeout"); \
    $finish; \
  end

`define AUTOSPEC_FINISH \
  begin \
    $display("PASSED: %0d FAILED: %0d", autospec_passed, autospec_failed); \
    if (autospec_failed == 0 && autospec_passed > 0) $display("PASS"); \
    else $display("FATAL: mismatch"); \
    $finish; \
  end

`endif
