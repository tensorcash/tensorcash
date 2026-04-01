# SPDX-License-Identifier: Apache-2.0
import chiavdf
block_hash =  '0000000000000000000000000000000000000000000000000000000000000000'
vdf =  '03006acd37874ed54b59686341ea45a6cc7f08c58977de1664f90f85bff42924af2cd7e16ba13e8e52391a4462cdd399a670d3f06227afe255f4cf81d15ce581340e7bcb85ea48b7cf4fc9266af725f21d85f58b281fc1d680e53d44b4b1ff2ea006010002000f31c61d60d93b930db712135c7980b4c9bc9c4a7a6ebfe11302eb01fe600025ac46c2e63c3bb0e8271f13cbe25c45a754a9dd897bcf165de172448a3d1f132f661bef57df7bb94ee5f26c7a44c4bfcf64dcd80eb31cbdd6aaa12a5083b9454a0100'
tick =  1998848

block_hash_bytes = bytes.fromhex(block_hash)
vdf_bytes = bytes.fromhex(vdf)

res = chiavdf.verify_from_hash(
            block_hash_bytes,
            vdf_bytes,
            1024,
            tick,
            0
        )

print(res)