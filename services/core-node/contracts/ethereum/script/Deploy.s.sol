// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Script.sol";
import "../src/TensorSwapHTLC.sol";

contract DeployHTLC is Script {
    function run() external {
        uint256 deployerPrivateKey = vm.envUint("DEPLOYER_KEY");
        vm.startBroadcast(deployerPrivateKey);

        TensorSwapHTLC htlc = new TensorSwapHTLC();

        vm.stopBroadcast();

        console.log("TensorSwapHTLC deployed at:", address(htlc));
    }
}
