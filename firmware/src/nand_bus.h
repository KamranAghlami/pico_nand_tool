/* NAND bus primitives over RP2040 SIO (mask operations only). */
#ifndef NAND_BUS_H
#define NAND_BUS_H

/* Put every NAND line in its safe idle state: CE#/WE#/RE# driven high, CLE/ALE driven low, IO and R/B# inputs.
 * Call first thing in main(). */
void nand_bus_init(void);

#endif
