// Type declarations for lib/cd-search.mjs (shared !cd / /cd helpers).
export declare const CD_CREATE_PREFIX: string;
export declare const CD_MIN_SCORE: number;
export declare const CD_MAX_CANDIDATES: number;
export declare const CD_MAX_SCANNED_ENTRIES: number;
export declare const CD_PRUNED_DIRS: Set<string>;

export declare function expandTilde(input: string): string;
export declare function isExistingDirectory(p: string): boolean;
/** Tilde-expanded; relative paths resolve from `cwd`. Null for empty input. */
export declare function resolveCdTarget(rawArg: string | null | undefined, cwd: string): string | null;
export declare function levenshtein(a: string, b: string): number;
/** Case-insensitive name similarity, 0-100. */
export declare function cdScoreName(name: string, targetName: string): number;
/** Directories resembling the target's basename, best score first. */
export declare function findSimilarDirs(target: string): string[];
/** Pre-write a minimal session header with cwd = targetCwd; returns the file path. */
export declare function writeCdSessionHeader(targetCwd: string): string;
