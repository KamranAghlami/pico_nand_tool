# Host-side firmware checks (no Pico SDK needed): unit tests for the pure modules + the NAND opcode safety gate.
#   make -C firmware/tests check
CC ?= gcc
CFLAGS ?= -std=c11 -Wall -Wextra -Werror -O1
SRC := ../src
OUT ?= build

.PHONY: check unit gate clean

check: unit gate

unit: $(OUT)/test_host
	./$(OUT)/test_host

$(OUT)/test_host: test_host.c $(SRC)/crc32.c $(SRC)/frame.c $(SRC)/timing.c $(wildcard $(SRC)/*.h)
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -I$(SRC) test_host.c $(SRC)/crc32.c $(SRC)/frame.c $(SRC)/timing.c -o $@

# Positive control must compile; forbidden and non-constant opcodes must NOT compile.
gate:
	@mkdir -p $(OUT)
	$(CC) $(CFLAGS) -fsyntax-only -I$(SRC) -DPOSITIVE_CONTROL forbidden_opcode.c
	@if $(CC) $(CFLAGS) -fsyntax-only -I$(SRC) forbidden_opcode.c 2> $(OUT)/gate.log; then \
		echo "FAIL: NAND_CMD(0x80) compiled - opcode gate is broken"; exit 1; fi
	@grep -q "forbidden NAND opcode" $(OUT)/gate.log || { cat $(OUT)/gate.log; echo "FAIL: wrong error"; exit 1; }
	@if $(CC) $(CFLAGS) -fsyntax-only -I$(SRC) -DNON_CONSTANT forbidden_opcode.c 2> $(OUT)/gate_nc.log; then \
		echo "FAIL: non-constant opcode compiled - opcode gate is broken"; exit 1; fi
	@echo "opcode gate: OK (allowed opcodes compile; 0x80 and non-constant opcodes are rejected)"

clean:
	rm -rf $(OUT)
