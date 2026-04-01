#!/usr/bin/env python3
"""Generate blocks to height 299 and print the actual hash for assumeutxo test."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'services/core-node/bcore/test/functional'))

from test_framework.test_framework import BitcoinTestFramework

class GetBlockHash299(BitcoinTestFramework):
    def set_test_params(self):
        self.num_nodes = 1
        self.setup_clean_chain = True

    def run_test(self):
        self.log.info("Generating blocks to height 299...")

        # Generate blocks to reach height 299
        self.generate(self.nodes[0], 299)

        # Get the block hash at height 299
        hash_299 = self.nodes[0].getblockhash(299)
        block_299 = self.nodes[0].getblock(hash_299)

        print("\n" + "="*80)
        print(f"Block at height 299:")
        print(f"  Hash: {hash_299}")
        print(f"  nChainTx: {block_299.get('nTx', 'N/A')}")

        # Also get hash at 200 and 110 for other test heights
        hash_200 = self.nodes[0].getblockhash(200)
        hash_110 = self.nodes[0].getblockhash(110)

        print(f"\nBlock at height 200:")
        print(f"  Hash: {hash_200}")

        print(f"\nBlock at height 110:")
        print(f"  Hash: {hash_110}")

        # Now create a UTXO snapshot at height 299
        self.log.info("Creating UTXO snapshot at height 299...")
        dump_output = self.nodes[0].dumptxoutset('utxos_299.dat', "latest")

        print(f"\nSnapshot at height 299:")
        print(f"  Hash serialized: {dump_output['txoutset_hash']}")
        print(f"  Base hash: {dump_output['base_hash']}")
        print(f"  nChainTx: {dump_output['nchaintx']}")
        print("="*80 + "\n")

        print("\nUpdate chainparams.cpp with these values:")
        print(f"  Height 299: blockhash = {hash_299}")
        print(f"             hash_serialized = {dump_output['txoutset_hash']}")
        print(f"             m_chain_tx_count = {dump_output['nchaintx']}")

if __name__ == '__main__':
    GetBlockHash299().main()