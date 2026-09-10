import { describe, it, expect } from "vitest";
import { getFeedPrice, hasFeedPrice } from "./priceFeed";

describe("getFeedPrice — strict real-feed price resolution", () => {
  it("returns the live quote price when present", () => {
    expect(getFeedPrice({ price: 24670.5 })).toBe(24670.5);
    expect(getFeedPrice({ price: 1.09 })).toBe(1.09);
  });

  it("NEVER returns the legacy hardcoded NIFTY fallback (24850.0) when no real quote exists", () => {
    expect(getFeedPrice(null)).not.toBe(24850.0);
    expect(getFeedPrice(undefined)).not.toBe(24850.0);
    expect(getFeedPrice({})).not.toBe(24850.0);
    expect(getFeedPrice({ price: 0 })).not.toBe(24850.0);
    expect(getFeedPrice({ price: null })).not.toBe(24850.0);
  });

  it("returns null while the quote is unknown / zero / invalid", () => {
    expect(getFeedPrice(null)).toBeNull();
    expect(getFeedPrice(undefined)).toBeNull();
    expect(getFeedPrice({})).toBeNull();
    expect(getFeedPrice({ price: 0 })).toBeNull();
    expect(getFeedPrice({ price: null })).toBeNull();
    expect(getFeedPrice({ price: NaN })).toBeNull();
    expect(getFeedPrice({ price: -5 })).toBeNull();
    expect(getFeedPrice({ price: "garbage" })).toBeNull();
  });

  it("honours an explicit positive fallback only while the quote is missing", () => {
    expect(getFeedPrice(null, 5000)).toBe(5000);
    expect(getFeedPrice({ price: 100 }, 5000)).toBe(100);
    expect(getFeedPrice({ price: 0 }, 5000)).toBe(5000);
  });

  it("hasFeedPrice reflects real feed availability", () => {
    expect(hasFeedPrice({ price: 10 })).toBe(true);
    expect(hasFeedPrice(null)).toBe(false);
    expect(hasFeedPrice({ price: 0 })).toBe(false);
    expect(hasFeedPrice(undefined)).toBe(false);
  });
});