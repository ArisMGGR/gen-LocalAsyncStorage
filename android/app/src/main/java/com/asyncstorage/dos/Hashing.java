package com.asyncstorage.dos;

/** sha256 helpers matching the server's hex digest format. */
public final class Hashing {
    private Hashing() {}

    public static String hex(byte[] digest) {
        StringBuilder sb = new StringBuilder(digest.length * 2);
        for (byte b : digest) {
            sb.append(Character.forDigit((b >> 4) & 0xF, 16));
            sb.append(Character.forDigit(b & 0xF, 16));
        }
        return sb.toString();
    }
}
