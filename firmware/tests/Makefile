# Host-side firmware checks (no Pico SDK needed): unit tests for the pure modules + the NAND opcode safety gate.
#   make -C firmware/tests check
# CFLAGS / CC from the environment or command line are honoured, but the required flags below are always applied.
REQ_CFLAGS := -std=c11 -Wall -Wextra -Werror
CFLAGS ?= -O1
SRC := ../src
OUT ?= build
BIN := $(abspath $(OUT))

.PHONY: check unit gate wp clean

check: unit gate wp

unit: $(BIN)/test_host
	$(BIN)/test_host

$(BIN)/test_host: test_host.c $(SRC)/crc32.c $(SRC)/frame.c $(SRC)/timing.c $(wildcard $(SRC)/*.h)
	@mkdir -p $(BIN)
	$(CC) $(REQ_CFLAGS) $(CFLAGS) -I$(SRC) test_host.c $(SRC)/crc32.c $(SRC)/frame.c $(SRC)/timing.c -o $@

# 1) allowed opcodes compile through their own gate (positive control), 2) NAND_CMD(0x80) and NAND_WCMD(0x85 / 0x70)
#    fail with the gate's message, 3) non-constant opcodes fail in the static assertion, 4) no direct latch calls and
#    no write path outside nand_write.c, and the call checker itself is proven to catch each.
GATE_CC = $(CC) $(REQ_CFLAGS) $(CFLAGS) -fsyntax-only -I$(SRC)
gate:
	@mkdir -p $(BIN)
	$(GATE_CC) -DPOSITIVE_CONTROL forbidden_opcode.c
	@set -e; for c in DEFAULT:forbidden.NAND.opcode WRITE_85:forbidden.NAND.write.opcode \
		WRITE_READ_OP:forbidden.NAND.write.opcode NON_CONSTANT:static.assert WRITE_NON_CONSTANT:static.assert; do \
		def=$${c%%:*}; want=$${c#*:}; \
		if $(GATE_CC) -D$$def forbidden_opcode.c 2> $(BIN)/gate_$$def.log; then \
			echo "FAIL: $$def compiled - opcode gate is broken"; exit 1; fi; \
		grep -Eqi "$$want" $(BIN)/gate_$$def.log || { cat $(BIN)/gate_$$def.log; \
			echo "FAIL: $$def rejected for the wrong reason (expected /$$want/)"; exit 1; }; \
	done
	@./check_gate_calls.sh $(SRC)
	@set -e; for sneaky in 'nand_bus_cmd_latch_(0x80);' 'NAND_WCMD(0x60);' 'nand_bus_write_window_open();' \
		'nand_bus_data_in(0, 0);'; do \
		rm -rf $(BIN)/badsrc && mkdir -p $(BIN)/badsrc && cp $(SRC)/*.h $(SRC)/*.c $(BIN)/badsrc/; \
		printf '#include "nand_cmd.h"\n#include "nand_bus.h"\nvoid sneaky(void) { %s }\n' "$$sneaky" \
			> $(BIN)/badsrc/sneaky.c; \
		if ./check_gate_calls.sh $(BIN)/badsrc > /dev/null; then \
			echo "FAIL: check_gate_calls.sh missed: $$sneaky"; exit 1; fi; \
	done
	@echo "opcode gate: OK (both gates compile their own opcodes; 80h via NAND_CMD, 85h/70h via NAND_WCMD and" \
		"non-constant opcodes are rejected; no direct latch calls; write path only in nand_write.c)"

# WP#: only pins.h and the allowed lines in nand_bus.c may name it, and it is raised exactly once, inside
# nand_bus_write_window_open(). The checker itself is proven to catch a raise in another file and a second raise in
# nand_bus.c.
wp:
	@./check_wp.sh $(SRC)
	@rm -rf $(BIN)/wpsrc && mkdir -p $(BIN)/wpsrc && cp $(SRC)/*.h $(SRC)/*.c $(BIN)/wpsrc/ && \
		printf '#include "pins.h"\nvoid sneaky(void) { gpio_set_mask(MASK_WP); }\n' > $(BIN)/wpsrc/sneaky.c
	@if ./check_wp.sh $(BIN)/wpsrc > /dev/null; then \
		echo "FAIL: check_wp.sh missed a WP# write in another file"; exit 1; fi
	@rm -rf $(BIN)/wpsrc && mkdir -p $(BIN)/wpsrc && cp $(SRC)/*.h $(SRC)/*.c $(BIN)/wpsrc/ && \
		printf 'void sneaky(void) {\n    gpio_set_mask(MASK_WP);\n}\n' >> $(BIN)/wpsrc/nand_bus.c
	@if ./check_wp.sh $(BIN)/wpsrc > /dev/null; then \
		echo "FAIL: check_wp.sh missed a second WP# raise in nand_bus.c"; exit 1; fi
	@echo "WP# guard: OK (raised only in nand_bus_write_window_open; any other use is rejected)"

clean:
	rm -rf $(BIN)
