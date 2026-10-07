// SPDX-License-Identifier: Apache-2.0
import Link from "next/link";

export default function NotFound() {
  return (
    <div className="empty">
      <h1>Not found</h1>
      <p>No pack or publisher by that name. <Link href="/">Search the packs</Link>.</p>
    </div>
  );
}
