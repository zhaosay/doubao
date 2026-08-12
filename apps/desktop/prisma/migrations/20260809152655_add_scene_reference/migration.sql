-- RedefineTables
PRAGMA defer_foreign_keys=ON;
PRAGMA foreign_keys=OFF;
CREATE TABLE "new_Scene" (
    "id" TEXT NOT NULL PRIMARY KEY,
    "storyId" TEXT NOT NULL,
    "order" INTEGER NOT NULL,
    "summary" TEXT NOT NULL,
    "refImagePath" TEXT,
    "status" TEXT NOT NULL DEFAULT 'pending',
    "error" TEXT,
    CONSTRAINT "Scene_storyId_fkey" FOREIGN KEY ("storyId") REFERENCES "Story" ("id") ON DELETE RESTRICT ON UPDATE CASCADE
);
INSERT INTO "new_Scene" ("id", "order", "storyId", "summary") SELECT "id", "order", "storyId", "summary" FROM "Scene";
DROP TABLE "Scene";
ALTER TABLE "new_Scene" RENAME TO "Scene";
PRAGMA foreign_keys=ON;
PRAGMA defer_foreign_keys=OFF;
