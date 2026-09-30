# Host-side firmware checks (no Pico SDK needed): unit tests for the pure modules (crc32, frame, timing, LED
# patterns) + the NAND opcode safety gate.
#   make -C firmware/tests check
# CFLAGS / CC from the environment or command line are honoured, but the required flags below are always applied.
REQ_CFLAGS := -std=c11 -Wall -Wextra -Werror
CFLAGS ?= -O1
SRC := ../src
OUT ?= build
BIN := $(abspath $(OUT))

.PHONY: check unit gate clean

check: unit gate

unit: $(BIN)/test_host
	$(BIN)/test_host

PURE_SRCS := $(SRC)/crc32.c $(SRC)/frame.c $(SRC)/timing.c $(SRC)/led_pattern.c

$(BIN)/test_host: test_host.c $(PURE_SRCS) $(wildcard $(SRC)/*.h)
	@mkdir -p $(BIN)
	$(CC) $(REQ_CFLAGS) $(CFLAGS) -I$(SRC) test_host.c $(PURE_SRCS) -o $@

# 1) allowed opcodes compile (positive control), 2) NAND_CMD(0x80) fails with the gate's message,
# 3) a non-constant opcode fails in the static assertion, 4) no direct calls to the latch, and the call checker
#    itself is proven to catch one.
gate:
	@mkdir -p $(BIN)
	$(CC) $(REQ_CFLAGS) $(CFLAGS) -fsyntax-only -I$(SRC) -DPOSITIVE_CONTROL forbidden_opcode.c
	@if $(CC) $(REQ_CFLAGS) $(CFLAGS) -fsyntax-only -I$(SRC) forbidden_opcode.c 2> $(BIN)/gate.log; then \
		echo "FAIL: NAND_CMD(0x80) compiled - opcode gate is broken"; exit 1; fi
	@grep -q "forbidden NAND opcode" $(BIN)/gate.log || { cat $(BIN)/gate.log; echo "FAIL: 0x80 rejected for the wrong reason"; exit 1; }
	@if $(CC) $(REQ_CFLAGS) $(CFLAGS) -fsyntax-only -I$(SRC) -DNON_CONSTANT forbidden_opcode.c 2> $(BIN)/gate_nc.log; then \
		echo "FAIL: non-constant opcode compiled - opcode gate is broken"; exit 1; fi
	@grep -Eqi "static.assert" $(BIN)/gate_nc.log || { cat $(BIN)/gate_nc.log; echo "FAIL: non-constant opcode rejected for the wrong reason"; exit 1; }
	@./check_gate_calls.sh $(SRC)
	@rm -rf $(BIN)/badsrc && mkdir -p $(BIN)/badsrc && cp $(SRC)/*.h $(SRC)/*.c $(BIN)/badsrc/ && \
		printf '#include "nand_cmd.h"\nvoid sneaky(void) { nand_bus_cmd_latch_(0x80); }\n' > $(BIN)/badsrc/sneaky.c
	@if ./check_gate_calls.sh $(BIN)/badsrc > /dev/null; then \
		echo "FAIL: check_gate_calls.sh missed a direct call"; exit 1; fi
	@echo "opcode gate: OK (allowed opcodes compile; 0x80, non-constant opcodes and direct latch calls are rejected)"

clean:
	rm -rf $(BIN)
