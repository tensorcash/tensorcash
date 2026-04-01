// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title ITensorSwapHTLC
/// @notice Interface for the TensorCash cross-chain HTLC contract.
///
/// This interface is the ABI boundary that the wallet claim builder,
/// oracle attestation format, and Tron port all depend on.  Changes
/// here require coordinated updates across all three.
interface ITensorSwapHTLC {
    // ---------------------------------------------------------------
    // Events
    // ---------------------------------------------------------------

    /// @notice Emitted when funds are locked into an HTLC.
    /// @param swapId       Unique swap identifier (deterministic from offer params).
    /// @param sender       Address that locked the funds (funder).
    /// @param recipient    Address that can claim with the correct secret.
    /// @param tokenAddress ERC-20 token address, or address(0) for native ETH.
    /// @param amount       Amount locked (wei for ETH, token base units for ERC-20).
    /// @param secretHash   sha256(secret) — NOT keccak256.
    /// @param timelock     Unix timestamp after which the sender can refund.
    event Locked(
        bytes32 indexed swapId,
        address indexed sender,
        address indexed recipient,
        address tokenAddress,
        uint256 amount,
        bytes32 secretHash,
        uint256 timelock
    );

    /// @notice Emitted when the recipient claims funds by revealing the secret.
    /// @param swapId    Swap identifier.
    /// @param secret    The preimage whose sha256 matches the stored secretHash.
    /// @param recipient Address that received the funds.
    event Claimed(
        bytes32 indexed swapId,
        bytes32 secret,
        address indexed recipient
    );

    /// @notice Emitted when the sender refunds after timelock expiry.
    /// @param swapId Swap identifier.
    /// @param sender Address that received the refund.
    event Refunded(
        bytes32 indexed swapId,
        address indexed sender
    );

    // ---------------------------------------------------------------
    // Errors
    // ---------------------------------------------------------------

    error SwapAlreadyExists(bytes32 swapId);
    error SwapNotLocked(bytes32 swapId);
    error InvalidRecipient();
    error InvalidTokenAddress();
    error ZeroAmount();
    error TimelockInPast(uint256 timelock, uint256 currentTime);
    error InvalidSecretHash();
    error InvalidSecret(bytes32 swapId);
    error TimelockNotExpired(bytes32 swapId, uint256 timelock, uint256 currentTime);
    error NotSender(bytes32 swapId, address caller, address expectedSender);
    error NativeTransferFailed();
    error TokenTransferFailed();

    // ---------------------------------------------------------------
    // Functions
    // ---------------------------------------------------------------

    /// @notice Lock native ETH into an HTLC.
    /// @param swapId     Unique swap identifier (must not already exist).
    /// @param recipient  Address that can claim with the secret.
    /// @param secretHash sha256(secret).
    /// @param timelock   Unix timestamp after which refund is allowed.
    function lock(
        bytes32 swapId,
        address recipient,
        bytes32 secretHash,
        uint256 timelock
    ) external payable;

    /// @notice Lock ERC-20 tokens into an HTLC.
    /// @dev Caller must have approved this contract for `amount` beforehand.
    /// @param swapId       Unique swap identifier (must not already exist).
    /// @param recipient    Address that can claim with the secret.
    /// @param tokenAddress ERC-20 token contract address.
    /// @param amount       Token amount in base units.
    /// @param secretHash   sha256(secret).
    /// @param timelock     Unix timestamp after which refund is allowed.
    function lockToken(
        bytes32 swapId,
        address recipient,
        address tokenAddress,
        uint256 amount,
        bytes32 secretHash,
        uint256 timelock
    ) external;

    /// @notice Claim locked funds by revealing the secret preimage.
    /// @dev Anyone can call this (the funds always go to the stored recipient).
    /// @param swapId Swap identifier.
    /// @param secret The 32-byte preimage such that sha256(secret) == secretHash.
    function claim(bytes32 swapId, bytes32 secret) external;

    /// @notice Refund locked funds after timelock expiry.
    /// @dev Only the original sender can call this.
    /// @param swapId Swap identifier.
    function refund(bytes32 swapId) external;

    /// @notice Query the state of a swap.
    /// @param swapId Swap identifier.
    /// @return state        0=EMPTY, 1=LOCKED, 2=CLAIMED, 3=REFUNDED
    /// @return sender       Address that locked the funds.
    /// @return recipient    Address that can claim.
    /// @return tokenAddress ERC-20 address or address(0) for native ETH.
    /// @return amount       Locked amount.
    /// @return secretHash   sha256(secret).
    /// @return timelock     Refund-eligible timestamp.
    function getSwap(bytes32 swapId)
        external
        view
        returns (
            uint8 state,
            address sender,
            address recipient,
            address tokenAddress,
            uint256 amount,
            bytes32 secretHash,
            uint256 timelock
        );
}
