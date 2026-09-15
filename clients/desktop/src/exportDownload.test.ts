import { describe, expect, it } from "vitest";

import { filenameFromContentDisposition } from "./exportDownload";

describe("filenameFromContentDisposition", () => {
  it("returns the fallback when the header is missing", () => {
    expect(filenameFromContentDisposition(null, "osprey-hotlist.xlsx")).toBe("osprey-hotlist.xlsx");
  });

  it("parses a plain filename= value", () => {
    expect(
      filenameFromContentDisposition(
        'attachment; filename="osprey-hotlist-Tower_B.xlsx"',
        "fallback.xlsx",
      ),
    ).toBe("osprey-hotlist-Tower_B.xlsx");
  });

  it("prefers RFC 5987 filename*", () => {
    expect(
      filenameFromContentDisposition(
        "attachment; filename=\"fallback.pdf\"; filename*=UTF-8''osprey-hotlist-Phase%202.pdf",
        "x.pdf",
      ),
    ).toBe("osprey-hotlist-Phase 2.pdf");
  });
});
