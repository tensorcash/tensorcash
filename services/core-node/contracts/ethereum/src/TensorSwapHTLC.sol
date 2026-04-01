// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "./IERC20.sol";
import {ITensorSwapHTLC} from "./ITensorSwapHTLC.sol";

/// @title TensorSwapHTLC
/// @notice Hash-Time-Locked Contract for TensorCash cross-chain settlement.
///
/// Design constraints from the TensorCash cross-chain plan:
///   - sha256 (NOT keccak256) for adaptor-secret compatibility with TSC/BTC
///   - timelock ordering enforced at lock time
///   - claim-after-refund and refund-after-claim are impossible
///   - duplicate swap IDs rejected
///   - reentrancy-safe token transfers (checks-effects-interactions)
///
/// Supports both native ETH and ERC-20 token locks.
contract TensorSwapHTLC is ITensorSwapHTLC {
    /// @dev Swap states — used as a uint8 discriminant, not a bitmap.
    enum SwapState {
        EMPTY,    // 0 — slot never initialized
        LOCKED,   // 1 — funds locked, claimable with secret
        CLAIMED,  // 2 — terminal: secret revealed, funds sent to recipient
        REFUNDED  // 3 — terminal: timelock expired, funds returned to sender
    }

    struct Swap {
        SwapState state;
        address sender;
        address recipient;
        address tokenAddress; // address(0) for native ETH
        uint256 amount;
        bytes32 secretHash;   // sha256(secret)
        uint256 timelock;     // block.timestamp after which refund is allowed
    }

    /// @dev swapId => Swap.  The swapId is caller-chosen (typically a
    ///      deterministic hash of the cross-chain offer parameters).
    mapping(bytes32 => Swap) private swaps;

    // ---------------------------------------------------------------
    // Modifiers
    // ---------------------------------------------------------------

    modifier onlyEmptySwap(bytes32 swapId) {
        if (swaps[swapId].state != SwapState.EMPTY) {
            revert SwapAlreadyExists(swapId);
        }
        _;
    }

    modifier onlyLockedSwap(bytes32 swapId) {
        if (swaps[swapId].state != SwapState.LOCKED) {
            revert SwapNotLocked(swapId);
        }
        _;
    }

    // ---------------------------------------------------------------
    // lock
    // ---------------------------------------------------------------

    /// @inheritdoc ITensorSwapHTLC
    function lock(
        bytes32 swapId,
        address recipient,
        bytes32 secretHash,
        uint256 timelock
    ) external payable override onlyEmptySwap(swapId) {
        if (recipient == address(0)) revert InvalidRecipient();
        if (msg.value == 0) revert ZeroAmount();
        if (timelock <= block.timestamp) revert TimelockInPast(timelock, block.timestamp);
        if (secretHash == bytes32(0)) revert InvalidSecretHash();

        swaps[swapId] = Swap({
            state: SwapState.LOCKED,
            sender: msg.sender,
            recipient: recipient,
            tokenAddress: address(0),
            amount: msg.value,
            secretHash: secretHash,
            timelock: timelock
        });

        emit Locked(swapId, msg.sender, recipient, address(0), msg.value, secretHash, timelock);
    }

    /// @inheritdoc ITensorSwapHTLC
    function lockToken(
        bytes32 swapId,
        address recipient,
        address tokenAddress,
        uint256 amount,
        bytes32 secretHash,
        uint256 timelock
    ) external override onlyEmptySwap(swapId) {
        if (recipient == address(0)) revert InvalidRecipient();
        if (tokenAddress == address(0)) revert InvalidTokenAddress();
        if (amount == 0) revert ZeroAmount();
        if (timelock <= block.timestamp) revert TimelockInPast(timelock, block.timestamp);
        if (secretHash == bytes32(0)) revert InvalidSecretHash();

        // Effects before interactions (CEI pattern)
        swaps[swapId] = Swap({
            state: SwapState.LOCKED,
            sender: msg.sender,
            recipient: recipient,
            tokenAddress: tokenAddress,
            amount: amount,
            secretHash: secretHash,
            timelock: timelock
        });

        // Interaction: pull tokens from sender
        // The caller must have approved this contract for `amount` beforehand.
        bool ok = IERC20(tokenAddress).transferFrom(msg.sender, address(this), amount);
        if (!ok) revert TokenTransferFailed();

        emit Locked(swapId, msg.sender, recipient, tokenAddress, amount, secretHash, timelock);
    }

    // ---------------------------------------------------------------
    // claim
    // ---------------------------------------------------------------

    /// @inheritdoc ITensorSwapHTLC
    function claim(
        bytes32 swapId,
        bytes32 secret
    ) external override onlyLockedSwap(swapId) {
        Swap storage s = swaps[swapId];

        // Verify the secret against the stored sha256 hash.
        // sha256 (not keccak256) is required for adaptor-secret compatibility.
        if (sha256(abi.encodePacked(secret)) != s.secretHash) {
            revert InvalidSecret(swapId);
        }

        // Effects before interactions
        s.state = SwapState.CLAIMED;
        address recipient = s.recipient;
        address tokenAddress = s.tokenAddress;
        uint256 amount = s.amount;

        // Interaction: send funds to recipient
        if (tokenAddress == address(0)) {
            // Native ETH
            (bool sent, ) = recipient.call{value: amount}("");
            if (!sent) revert NativeTransferFailed();
        } else {
            bool ok = IERC20(tokenAddress).transfer(recipient, amount);
            if (!ok) revert TokenTransferFailed();
        }

        emit Claimed(swapId, secret, recipient);
    }

    // ---------------------------------------------------------------
    // refund
    // ---------------------------------------------------------------

    /// @inheritdoc ITensorSwapHTLC
    function refund(bytes32 swapId) external override onlyLockedSwap(swapId) {
        Swap storage s = swaps[swapId];

        if (block.timestamp < s.timelock) {
            revert TimelockNotExpired(swapId, s.timelock, block.timestamp);
        }

        // Only the original sender can refund
        if (msg.sender != s.sender) {
            revert NotSender(swapId, msg.sender, s.sender);
        }

        // Effects before interactions
        s.state = SwapState.REFUNDED;
        address sender = s.sender;
        address tokenAddress = s.tokenAddress;
        uint256 amount = s.amount;

        // Interaction: return funds to sender
        if (tokenAddress == address(0)) {
            (bool sent, ) = sender.call{value: amount}("");
            if (!sent) revert NativeTransferFailed();
        } else {
            bool ok = IERC20(tokenAddress).transfer(sender, amount);
            if (!ok) revert TokenTransferFailed();
        }

        emit Refunded(swapId, sender);
    }

    // ---------------------------------------------------------------
    // getSwap
    // ---------------------------------------------------------------

    /// @inheritdoc ITensorSwapHTLC
    function getSwap(bytes32 swapId)
        external
        view
        override
        returns (
            uint8 state,
            address sender,
            address recipient,
            address tokenAddress,
            uint256 amount,
            bytes32 secretHash,
            uint256 timelock
        )
    {
        Swap storage s = swaps[swapId];
        return (
            uint8(s.state),
            s.sender,
            s.recipient,
            s.tokenAddress,
            s.amount,
            s.secretHash,
            s.timelock
        );
    }
}
