# Minimal QR Code encoder for MicroPython
# Supports versions 1-6, error correction level L, byte mode

# QR version capacities (byte mode, EC level L)
_CAPACITIES = [0, 17, 32, 53, 78, 106, 134]

# Number of data codewords per version (EC level L)
_DATA_CODEWORDS = [0, 19, 34, 55, 80, 108, 136]

# EC codewords per block (EC level L)
_EC_CODEWORDS = [0, 7, 10, 15, 20, 26, 18]

# Number of blocks (EC level L)
_NUM_BLOCKS = [0, 1, 1, 1, 1, 1, 2]

# Alignment pattern positions
_ALIGN_POS = [[], [], [6, 18], [6, 22], [6, 26], [6, 30], [6, 34]]

# Format info strings for EC level L (mask 0-7)
_FORMAT_BITS = [
    0x77C4, 0x72F3, 0x7DAA, 0x789D, 0x662F, 0x6318, 0x6C41, 0x6976
]

# GF(256) log/exp tables for Reed-Solomon
_EXP = [0] * 256
_LOG = [0] * 256

def _init_gf():
    val = 1
    for i in range(255):
        _EXP[i] = val
        _LOG[val] = i
        val <<= 1
        if val >= 256:
            val ^= 0x11D
    _EXP[255] = _EXP[0]

_init_gf()


def _gf_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return _EXP[(_LOG[a] + _LOG[b]) % 255]


def _rs_generator(n):
    """Generate Reed-Solomon generator polynomial of degree n."""
    g = [1]
    for i in range(n):
        ng = [0] * (len(g) + 1)
        for j in range(len(g)):
            ng[j] ^= g[j]
            ng[j + 1] ^= _gf_mul(g[j], _EXP[i])
        g = ng
    return g


def _rs_encode(data, n_ec):
    """Encode data with Reed-Solomon error correction."""
    gen = _rs_generator(n_ec)
    result = list(data) + [0] * n_ec
    for i in range(len(data)):
        coef = result[i]
        if coef != 0:
            for j in range(len(gen)):
                result[i + j] ^= _gf_mul(gen[j], coef)
    return result[len(data):]


def _get_version(data_len):
    """Find minimum QR version for data length."""
    for v in range(1, 7):
        if data_len <= _CAPACITIES[v]:
            return v
    raise ValueError("Data too long")


def encode(text):
    """Encode text into a QR code matrix. Returns list of lists of bools."""
    data = text.encode('utf-8') if isinstance(text, str) else text
    version = _get_version(len(data))
    size = version * 4 + 17

    # Encode data
    n_data = _DATA_CODEWORDS[version]
    n_ec = _EC_CODEWORDS[version]

    # Build data stream: mode(4) + length(8 or 16) + data + terminator + padding
    bits = []

    # Mode indicator: byte mode = 0100
    bits.extend([0, 1, 0, 0])

    # Character count (8 bits for versions 1-9)
    count_bits = 8 if version <= 9 else 16
    for i in range(count_bits - 1, -1, -1):
        bits.append((len(data) >> i) & 1)

    # Data
    for byte in data:
        for i in range(7, -1, -1):
            bits.append((byte >> i) & 1)

    # Terminator (up to 4 zeros)
    term_len = min(4, n_data * 8 - len(bits))
    bits.extend([0] * term_len)

    # Pad to byte boundary
    while len(bits) % 8 != 0:
        bits.append(0)

    # Pad with alternating bytes
    pad_bytes = [0xEC, 0x11]
    pad_idx = 0
    while len(bits) < n_data * 8:
        for i in range(7, -1, -1):
            bits.append((pad_bytes[pad_idx] >> i) & 1)
        pad_idx = (pad_idx + 1) % 2

    # Convert to codewords
    codewords = []
    for i in range(0, len(bits), 8):
        byte = 0
        for j in range(8):
            if i + j < len(bits):
                byte = (byte << 1) | bits[i + j]
        codewords.append(byte)

    # Split into blocks and add EC
    num_blocks = _NUM_BLOCKS[version]
    block_size = n_data // num_blocks
    data_blocks = []
    ec_blocks = []
    offset = 0
    for b in range(num_blocks):
        bs = block_size + (1 if b >= num_blocks - (n_data % num_blocks) else 0)
        block_data = codewords[offset:offset + bs]
        data_blocks.append(block_data)
        ec_blocks.append(_rs_encode(block_data, n_ec))
        offset += bs

    # Interleave blocks
    final = []
    max_data = max(len(b) for b in data_blocks)
    for i in range(max_data):
        for b in data_blocks:
            if i < len(b):
                final.append(b[i])
    for i in range(n_ec):
        for b in ec_blocks:
            if i < len(b):
                final.append(b[i])

    # Create matrix
    matrix = [[None] * size for _ in range(size)]
    reserved = [[False] * size for _ in range(size)]

    # Place finder patterns
    _place_finder(matrix, reserved, 0, 0, size)
    _place_finder(matrix, reserved, size - 7, 0, size)
    _place_finder(matrix, reserved, 0, size - 7, size)

    # Place alignment patterns
    if version >= 2:
        positions = _ALIGN_POS[version]
        for r in positions:
            for c in positions:
                if not reserved[r][c]:
                    _place_alignment(matrix, reserved, r, c, size)

    # Place timing patterns
    for i in range(8, size - 8):
        if not reserved[i][6]:
            matrix[i][6] = (i % 2 == 0)
            reserved[i][6] = True
        if not reserved[6][i]:
            matrix[6][i] = (i % 2 == 0)
            reserved[6][i] = True

    # Reserve format info areas
    for i in range(9):
        if i < size:
            reserved[i][8] = True
            reserved[8][i] = True
    for i in range(8):
        if size - 1 - i < size:
            reserved[size - 1 - i][8] = True
            reserved[8][size - 1 - i] = True
    # Dark module
    matrix[size - 8][8] = True
    reserved[size - 8][8] = True

    # Place data bits
    _place_data(matrix, reserved, final, size)

    # Apply best mask
    best_mask = 0
    best_score = None
    for mask_id in range(8):
        test = [row[:] for row in matrix]
        _apply_mask(test, reserved, mask_id, size)
        _place_format(test, mask_id, size)
        score = _score(test, size)
        if best_score is None or score < best_score:
            best_score = score
            best_mask = mask_id

    _apply_mask(matrix, reserved, best_mask, size)
    _place_format(matrix, best_mask, size)

    # Convert None to False
    for r in range(size):
        for c in range(size):
            if matrix[r][c] is None:
                matrix[r][c] = False

    return matrix


def _place_finder(matrix, reserved, row, col, size):
    """Place a finder pattern."""
    for r in range(-1, 8):
        for c in range(-1, 8):
            rr, cc = row + r, col + c
            if 0 <= rr < size and 0 <= cc < size:
                if 0 <= r <= 6 and 0 <= c <= 6:
                    if (r in (0, 6) or c in (0, 6) or
                            (2 <= r <= 4 and 2 <= c <= 4)):
                        matrix[rr][cc] = True
                    else:
                        matrix[rr][cc] = False
                else:
                    matrix[rr][cc] = False
                reserved[rr][cc] = True


def _place_alignment(matrix, reserved, row, col, size):
    """Place an alignment pattern."""
    for r in range(-2, 3):
        for c in range(-2, 3):
            rr, cc = row + r, col + c
            if 0 <= rr < size and 0 <= cc < size:
                if abs(r) == 2 or abs(c) == 2 or (r == 0 and c == 0):
                    matrix[rr][cc] = True
                else:
                    matrix[rr][cc] = False
                reserved[rr][cc] = True


def _place_data(matrix, reserved, data, size):
    """Place data bits in the matrix using the QR zigzag pattern."""
    bits = []
    for byte in data:
        for i in range(7, -1, -1):
            bits.append((byte >> i) & 1)

    bit_idx = 0
    col = size - 1
    going_up = True

    while col >= 0:
        if col == 6:  # Skip timing column
            col -= 1
            continue

        if going_up:
            for row in range(size - 1, -1, -1):
                for dc in range(2):
                    c = col - dc
                    if 0 <= c < size and not reserved[row][c]:
                        if bit_idx < len(bits):
                            matrix[row][c] = bool(bits[bit_idx])
                            bit_idx += 1
                        else:
                            matrix[row][c] = False
        else:
            for row in range(size):
                for dc in range(2):
                    c = col - dc
                    if 0 <= c < size and not reserved[row][c]:
                        if bit_idx < len(bits):
                            matrix[row][c] = bool(bits[bit_idx])
                            bit_idx += 1
                        else:
                            matrix[row][c] = False

        going_up = not going_up
        col -= 2


def _apply_mask(matrix, reserved, mask_id, size):
    """Apply a mask pattern."""
    for r in range(size):
        for c in range(size):
            if not reserved[r][c]:
                mask = False
                if mask_id == 0:
                    mask = (r + c) % 2 == 0
                elif mask_id == 1:
                    mask = r % 2 == 0
                elif mask_id == 2:
                    mask = c % 3 == 0
                elif mask_id == 3:
                    mask = (r + c) % 3 == 0
                elif mask_id == 4:
                    mask = (r // 2 + c // 3) % 2 == 0
                elif mask_id == 5:
                    mask = (r * c) % 2 + (r * c) % 3 == 0
                elif mask_id == 6:
                    mask = ((r * c) % 2 + (r * c) % 3) % 2 == 0
                elif mask_id == 7:
                    mask = ((r + c) % 2 + (r * c) % 3) % 2 == 0
                if mask:
                    matrix[r][c] = not matrix[r][c]


def _place_format(matrix, mask_id, size):
    """Place format information."""
    fmt = _FORMAT_BITS[mask_id]
    # First copy - around top-left finder
    # Vertical: bits 0-5 at rows 0-5, bit 6 at row 7, bit 7 at row 8
    for i in range(6):
        matrix[i][8] = bool((fmt >> i) & 1)
    matrix[7][8] = bool((fmt >> 6) & 1)
    matrix[8][8] = bool((fmt >> 7) & 1)
    # Horizontal: bit 8 at col 7, bits 9-14 at cols 5-0
    matrix[8][7] = bool((fmt >> 8) & 1)
    for i in range(6):
        matrix[8][5 - i] = bool((fmt >> (9 + i)) & 1)
    # Second copy - around other finders
    # Horizontal: bits 0-7 at cols size-1 to size-8
    for i in range(8):
        matrix[8][size - 1 - i] = bool((fmt >> i) & 1)
    # Vertical: bits 8-14 at rows size-7 to size-1
    for i in range(7):
        matrix[size - 7 + i][8] = bool((fmt >> (8 + i)) & 1)


def _score(matrix, size):
    """Calculate penalty score for mask selection."""
    score = 0
    # Rule 1: consecutive same-color modules in row/col
    for r in range(size):
        count = 1
        for c in range(1, size):
            if matrix[r][c] == matrix[r][c - 1]:
                count += 1
            else:
                if count >= 5:
                    score += count - 2
                count = 1
        if count >= 5:
            score += count - 2
    for c in range(size):
        count = 1
        for r in range(1, size):
            if matrix[r][c] == matrix[r - 1][c]:
                count += 1
            else:
                if count >= 5:
                    score += count - 2
                count = 1
        if count >= 5:
            score += count - 2
    # Rule 2: 2x2 blocks
    for r in range(size - 1):
        for c in range(size - 1):
            val = matrix[r][c]
            if matrix[r][c+1] == val and matrix[r+1][c] == val and matrix[r+1][c+1] == val:
                score += 3
    return score
