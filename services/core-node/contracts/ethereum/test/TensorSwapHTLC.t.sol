// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {TensorSwapHTLC} from "../src/TensorSwapHTLC.sol";
import {ITensorSwapHTLC} from "../src/ITensorSwapHTLC.sol";

/// @dev Minimal ERC-20 token for testing.
contract MockERC20 {
    string public name = "Mock Token";
    string public symbol = "MOCK";
    uint8 public decimals = 18;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    uint256 public totalSupply;

    function mint(address to, uint256 amount) external {
        balanceOf[to] += amount;
        totalSupply += amount;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        return true;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        require(balanceOf[msg.sender] >= amount, "insufficient");
        balanceOf[msg.sender] -= amount;
        balanceOf[to] += amount;
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        require(allowance[from][msg.sender] >= amount, "allowance");
        require(balanceOf[from] >= amount, "insufficient");
        allowance[from][msg.sender] -= amount;
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        return true;
    }
}

contract TensorSwapHTLCTest is Test {
    TensorSwapHTLC public htlc;
    MockERC20 public token;

    address public alice = makeAddr("alice"); // sender / funder
    address public bob   = makeAddr("bob");   // recipient / claimer

    bytes32 public constant SECRET = bytes32(uint256(0xdeadbeef));
    bytes32 public SECRET_HASH; // sha256(SECRET), computed in setUp

    uint256 public constant LOCK_AMOUNT = 1 ether;
    uint256 public constant TOKEN_AMOUNT = 1000e18;
    uint256 public constant TIMELOCK_DELTA = 1 days;

    function setUp() public {
        htlc = new TensorSwapHTLC();
        token = new MockERC20();

        // Compute sha256(secret) — matches the contract's hash function
        SECRET_HASH = sha256(abi.encodePacked(SECRET));

        // Fund alice
        vm.deal(alice, 10 ether);
        token.mint(alice, 10000e18);

        // Alice approves HTLC for token transfers
        vm.prank(alice);
        token.approve(address(htlc), type(uint256).max);
    }

    // ---------------------------------------------------------------
    // Helpers
    // ---------------------------------------------------------------

    function _lockETH(bytes32 swapId) internal {
        vm.prank(alice);
        htlc.lock{value: LOCK_AMOUNT}(
            swapId, bob, SECRET_HASH, block.timestamp + TIMELOCK_DELTA
        );
    }

    function _lockToken(bytes32 swapId) internal {
        vm.prank(alice);
        htlc.lockToken(
            swapId, bob, address(token), TOKEN_AMOUNT,
            SECRET_HASH, block.timestamp + TIMELOCK_DELTA
        );
    }

    function _swapId(string memory label) internal pure returns (bytes32) {
        return keccak256(abi.encodePacked(label));
    }

    // ---------------------------------------------------------------
    // lock — native ETH
    // ---------------------------------------------------------------

    function test_lock_eth_happy() public {
        bytes32 id = _swapId("eth-1");
        vm.expectEmit(true, true, true, true);
        emit ITensorSwapHTLC.Locked(
            id, alice, bob, address(0), LOCK_AMOUNT,
            SECRET_HASH, block.timestamp + TIMELOCK_DELTA
        );

        _lockETH(id);

        (uint8 state, address sender, address recipient,
         address tokenAddr, uint256 amount, bytes32 hash,
         uint256 timelock) = htlc.getSwap(id);

        assertEq(state, 1); // LOCKED
        assertEq(sender, alice);
        assertEq(recipient, bob);
        assertEq(tokenAddr, address(0));
        assertEq(amount, LOCK_AMOUNT);
        assertEq(hash, SECRET_HASH);
        assertEq(timelock, block.timestamp + TIMELOCK_DELTA);
        assertEq(address(htlc).balance, LOCK_AMOUNT);
    }

    function test_lock_eth_rejects_zero_value() public {
        bytes32 id = _swapId("eth-zero");
        vm.prank(alice);
        vm.expectRevert(ITensorSwapHTLC.ZeroAmount.selector);
        htlc.lock{value: 0}(id, bob, SECRET_HASH, block.timestamp + TIMELOCK_DELTA);
    }

    function test_lock_eth_rejects_zero_recipient() public {
        bytes32 id = _swapId("eth-norec");
        vm.prank(alice);
        vm.expectRevert(ITensorSwapHTLC.InvalidRecipient.selector);
        htlc.lock{value: LOCK_AMOUNT}(id, address(0), SECRET_HASH, block.timestamp + TIMELOCK_DELTA);
    }

    function test_lock_eth_rejects_past_timelock() public {
        bytes32 id = _swapId("eth-past");
        vm.prank(alice);
        vm.expectRevert(
            abi.encodeWithSelector(
                ITensorSwapHTLC.TimelockInPast.selector,
                block.timestamp - 1,
                block.timestamp
            )
        );
        htlc.lock{value: LOCK_AMOUNT}(id, bob, SECRET_HASH, block.timestamp - 1);
    }

    function test_lock_eth_rejects_zero_secret_hash() public {
        bytes32 id = _swapId("eth-nohash");
        vm.prank(alice);
        vm.expectRevert(ITensorSwapHTLC.InvalidSecretHash.selector);
        htlc.lock{value: LOCK_AMOUNT}(id, bob, bytes32(0), block.timestamp + TIMELOCK_DELTA);
    }

    function test_lock_eth_rejects_duplicate_swap_id() public {
        bytes32 id = _swapId("eth-dup");
        _lockETH(id);

        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSelector(ITensorSwapHTLC.SwapAlreadyExists.selector, id));
        htlc.lock{value: LOCK_AMOUNT}(id, bob, SECRET_HASH, block.timestamp + TIMELOCK_DELTA);
    }

    // ---------------------------------------------------------------
    // lock — ERC-20 token
    // ---------------------------------------------------------------

    function test_lock_token_happy() public {
        bytes32 id = _swapId("tok-1");
        uint256 aliceBefore = token.balanceOf(alice);

        vm.expectEmit(true, true, true, true);
        emit ITensorSwapHTLC.Locked(
            id, alice, bob, address(token), TOKEN_AMOUNT,
            SECRET_HASH, block.timestamp + TIMELOCK_DELTA
        );

        _lockToken(id);

        (uint8 state,,, address tokenAddr, uint256 amount,,) = htlc.getSwap(id);
        assertEq(state, 1);
        assertEq(tokenAddr, address(token));
        assertEq(amount, TOKEN_AMOUNT);
        assertEq(token.balanceOf(address(htlc)), TOKEN_AMOUNT);
        assertEq(token.balanceOf(alice), aliceBefore - TOKEN_AMOUNT);
    }

    function test_lock_token_rejects_zero_token_address() public {
        bytes32 id = _swapId("tok-noaddr");
        vm.prank(alice);
        vm.expectRevert(ITensorSwapHTLC.InvalidTokenAddress.selector);
        htlc.lockToken(id, bob, address(0), TOKEN_AMOUNT, SECRET_HASH, block.timestamp + TIMELOCK_DELTA);
    }

    function test_lock_token_rejects_zero_amount() public {
        bytes32 id = _swapId("tok-zero");
        vm.prank(alice);
        vm.expectRevert(ITensorSwapHTLC.ZeroAmount.selector);
        htlc.lockToken(id, bob, address(token), 0, SECRET_HASH, block.timestamp + TIMELOCK_DELTA);
    }

    function test_lock_token_rejects_duplicate() public {
        bytes32 id = _swapId("tok-dup");
        _lockToken(id);

        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSelector(ITensorSwapHTLC.SwapAlreadyExists.selector, id));
        htlc.lockToken(id, bob, address(token), TOKEN_AMOUNT, SECRET_HASH, block.timestamp + TIMELOCK_DELTA);
    }

    // ---------------------------------------------------------------
    // claim
    // ---------------------------------------------------------------

    function test_claim_eth_happy() public {
        bytes32 id = _swapId("claim-eth");
        _lockETH(id);

        uint256 bobBefore = bob.balance;

        vm.expectEmit(true, true, true, true);
        emit ITensorSwapHTLC.Claimed(id, SECRET, bob);

        vm.prank(bob);
        htlc.claim(id, SECRET);

        (uint8 state,,,,,,) = htlc.getSwap(id);
        assertEq(state, 2); // CLAIMED
        assertEq(bob.balance, bobBefore + LOCK_AMOUNT);
    }

    function test_claim_token_happy() public {
        bytes32 id = _swapId("claim-tok");
        _lockToken(id);

        uint256 bobBefore = token.balanceOf(bob);

        vm.prank(bob);
        htlc.claim(id, SECRET);

        (uint8 state,,,,,,) = htlc.getSwap(id);
        assertEq(state, 2);
        assertEq(token.balanceOf(bob), bobBefore + TOKEN_AMOUNT);
    }

    function test_claim_anyone_can_call() public {
        bytes32 id = _swapId("claim-anyone");
        _lockETH(id);

        // A third party submits the claim — funds still go to bob
        address charlie = makeAddr("charlie");
        uint256 bobBefore = bob.balance;

        vm.prank(charlie);
        htlc.claim(id, SECRET);

        assertEq(bob.balance, bobBefore + LOCK_AMOUNT);
    }

    function test_claim_rejects_wrong_secret() public {
        bytes32 id = _swapId("claim-wrong");
        _lockETH(id);

        bytes32 wrongSecret = bytes32(uint256(0xcafebabe));

        vm.prank(bob);
        vm.expectRevert(abi.encodeWithSelector(ITensorSwapHTLC.InvalidSecret.selector, id));
        htlc.claim(id, wrongSecret);
    }

    function test_claim_rejects_empty_swap() public {
        bytes32 id = _swapId("claim-empty");
        vm.prank(bob);
        vm.expectRevert(abi.encodeWithSelector(ITensorSwapHTLC.SwapNotLocked.selector, id));
        htlc.claim(id, SECRET);
    }

    function test_claim_rejects_already_claimed() public {
        bytes32 id = _swapId("claim-double");
        _lockETH(id);

        vm.prank(bob);
        htlc.claim(id, SECRET);

        vm.prank(bob);
        vm.expectRevert(abi.encodeWithSelector(ITensorSwapHTLC.SwapNotLocked.selector, id));
        htlc.claim(id, SECRET);
    }

    function test_claim_rejects_after_refund() public {
        bytes32 id = _swapId("claim-after-refund");
        _lockETH(id);

        // Fast-forward past timelock and refund
        vm.warp(block.timestamp + TIMELOCK_DELTA + 1);
        vm.prank(alice);
        htlc.refund(id);

        // Claim must fail
        vm.prank(bob);
        vm.expectRevert(abi.encodeWithSelector(ITensorSwapHTLC.SwapNotLocked.selector, id));
        htlc.claim(id, SECRET);
    }

    // ---------------------------------------------------------------
    // refund
    // ---------------------------------------------------------------

    function test_refund_eth_happy() public {
        bytes32 id = _swapId("refund-eth");
        _lockETH(id);

        uint256 aliceBefore = alice.balance;

        vm.warp(block.timestamp + TIMELOCK_DELTA + 1);

        vm.expectEmit(true, true, true, true);
        emit ITensorSwapHTLC.Refunded(id, alice);

        vm.prank(alice);
        htlc.refund(id);

        (uint8 state,,,,,,) = htlc.getSwap(id);
        assertEq(state, 3); // REFUNDED
        assertEq(alice.balance, aliceBefore + LOCK_AMOUNT);
    }

    function test_refund_token_happy() public {
        bytes32 id = _swapId("refund-tok");
        _lockToken(id);

        uint256 aliceBefore = token.balanceOf(alice);

        vm.warp(block.timestamp + TIMELOCK_DELTA + 1);
        vm.prank(alice);
        htlc.refund(id);

        (uint8 state,,,,,,) = htlc.getSwap(id);
        assertEq(state, 3);
        assertEq(token.balanceOf(alice), aliceBefore + TOKEN_AMOUNT);
    }

    function test_refund_rejects_before_timelock() public {
        bytes32 id = _swapId("refund-early");
        _lockETH(id);

        vm.prank(alice);
        vm.expectRevert(
            abi.encodeWithSelector(
                ITensorSwapHTLC.TimelockNotExpired.selector,
                id,
                block.timestamp + TIMELOCK_DELTA,
                block.timestamp
            )
        );
        htlc.refund(id);
    }

    function test_refund_rejects_non_sender() public {
        bytes32 id = _swapId("refund-notsender");
        _lockETH(id);

        vm.warp(block.timestamp + TIMELOCK_DELTA + 1);
        vm.prank(bob); // bob is recipient, not sender
        vm.expectRevert(
            abi.encodeWithSelector(ITensorSwapHTLC.NotSender.selector, id, bob, alice)
        );
        htlc.refund(id);
    }

    function test_refund_rejects_after_claim() public {
        bytes32 id = _swapId("refund-after-claim");
        _lockETH(id);

        vm.prank(bob);
        htlc.claim(id, SECRET);

        vm.warp(block.timestamp + TIMELOCK_DELTA + 1);
        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSelector(ITensorSwapHTLC.SwapNotLocked.selector, id));
        htlc.refund(id);
    }

    function test_refund_rejects_double_refund() public {
        bytes32 id = _swapId("refund-double");
        _lockETH(id);

        vm.warp(block.timestamp + TIMELOCK_DELTA + 1);
        vm.prank(alice);
        htlc.refund(id);

        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSelector(ITensorSwapHTLC.SwapNotLocked.selector, id));
        htlc.refund(id);
    }

    // ---------------------------------------------------------------
    // getSwap
    // ---------------------------------------------------------------

    function test_getSwap_empty() public view {
        bytes32 id = _swapId("nonexistent");
        (uint8 state,,,,,,) = htlc.getSwap(id);
        assertEq(state, 0); // EMPTY
    }

    // ---------------------------------------------------------------
    // sha256 compatibility — the critical invariant
    // ---------------------------------------------------------------

    function test_sha256_not_keccak256() public {
        // Verify the contract uses sha256, not keccak256.
        // If keccak256 were used, this secret would fail to claim
        // because we computed SECRET_HASH with sha256 in setUp().
        bytes32 id = _swapId("sha256-check");
        _lockETH(id);

        // This must succeed — proves sha256 is used
        vm.prank(bob);
        htlc.claim(id, SECRET);

        // Verify: keccak256(SECRET) != sha256(SECRET)
        bytes32 keccakHash = keccak256(abi.encodePacked(SECRET));
        assertTrue(keccakHash != SECRET_HASH, "sha256 and keccak256 should differ");
    }

    // ---------------------------------------------------------------
    // Reentrancy safety
    // ---------------------------------------------------------------

    function test_claim_reentrancy_safe() public {
        // Deploy a malicious recipient that tries to re-claim on receive
        ReentrantClaimer attacker = new ReentrantClaimer(htlc);
        bytes32 id = _swapId("reentrant");

        vm.prank(alice);
        htlc.lock{value: LOCK_AMOUNT}(
            id, address(attacker), SECRET_HASH, block.timestamp + TIMELOCK_DELTA
        );

        // The attacker will try to call claim again in its receive()
        attacker.setAttackParams(id, SECRET);

        // Claim should succeed once — the re-entrant call reverts with SwapNotLocked
        // because state is set to CLAIMED before the ETH transfer
        vm.prank(address(attacker));
        htlc.claim(id, SECRET);

        (uint8 state,,,,,,) = htlc.getSwap(id);
        assertEq(state, 2); // CLAIMED
        assertEq(address(attacker).balance, LOCK_AMOUNT);
        assertTrue(attacker.reentrancyAttempted());
        assertTrue(attacker.reentrancyFailed());
    }

    // ---------------------------------------------------------------
    // Multiple independent swaps
    // ---------------------------------------------------------------

    function test_multiple_independent_swaps() public {
        bytes32 id1 = _swapId("multi-1");
        bytes32 id2 = _swapId("multi-2");

        bytes32 secret2 = bytes32(uint256(0x12345678));
        bytes32 secretHash2 = sha256(abi.encodePacked(secret2));

        // Lock two independent swaps
        vm.prank(alice);
        htlc.lock{value: 0.5 ether}(id1, bob, SECRET_HASH, block.timestamp + TIMELOCK_DELTA);

        vm.prank(alice);
        htlc.lock{value: 0.3 ether}(id2, bob, secretHash2, block.timestamp + 2 days);

        // Claim first, refund second
        vm.prank(bob);
        htlc.claim(id1, SECRET);

        vm.warp(block.timestamp + 3 days);
        vm.prank(alice);
        htlc.refund(id2);

        (uint8 s1,,,,,,) = htlc.getSwap(id1);
        (uint8 s2,,,,,,) = htlc.getSwap(id2);
        assertEq(s1, 2); // CLAIMED
        assertEq(s2, 3); // REFUNDED
    }

    // ---------------------------------------------------------------
    // Edge: timelock exactly at current time
    // ---------------------------------------------------------------

    function test_lock_rejects_timelock_equal_to_now() public {
        bytes32 id = _swapId("timelock-exact");
        vm.prank(alice);
        vm.expectRevert(
            abi.encodeWithSelector(
                ITensorSwapHTLC.TimelockInPast.selector,
                block.timestamp,
                block.timestamp
            )
        );
        htlc.lock{value: LOCK_AMOUNT}(id, bob, SECRET_HASH, block.timestamp);
    }

    function test_refund_at_exact_timelock() public {
        bytes32 id = _swapId("refund-exact");
        uint256 tl = block.timestamp + TIMELOCK_DELTA;
        vm.prank(alice);
        htlc.lock{value: LOCK_AMOUNT}(id, bob, SECRET_HASH, tl);

        // One second before timelock — must fail
        vm.warp(tl - 1);
        vm.prank(alice);
        vm.expectRevert(
            abi.encodeWithSelector(
                ITensorSwapHTLC.TimelockNotExpired.selector, id, tl, tl - 1
            )
        );
        htlc.refund(id);

        // At exactly timelock — refund is allowed (block.timestamp >= timelock)
        vm.warp(tl);
        vm.prank(alice);
        htlc.refund(id);

        (uint8 state,,,,,,) = htlc.getSwap(id);
        assertEq(state, 3);
    }
}

/// @dev Helper contract that attempts reentrancy on ETH receive.
contract ReentrantClaimer {
    TensorSwapHTLC public htlc;
    bytes32 public attackSwapId;
    bytes32 public attackSecret;
    bool public reentrancyAttempted;
    bool public reentrancyFailed;

    constructor(TensorSwapHTLC _htlc) {
        htlc = _htlc;
    }

    function setAttackParams(bytes32 swapId, bytes32 secret) external {
        attackSwapId = swapId;
        attackSecret = secret;
    }

    receive() external payable {
        if (!reentrancyAttempted) {
            reentrancyAttempted = true;
            try htlc.claim(attackSwapId, attackSecret) {
                // If this succeeds, the contract is vulnerable
            } catch {
                reentrancyFailed = true;
            }
        }
    }
}
