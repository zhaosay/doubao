-- RedefineTables
PRAGMA defer_foreign_keys=ON;
PRAGMA foreign_keys=OFF;
CREATE TABLE "new_Setting" (
    "id" TEXT NOT NULL PRIMARY KEY DEFAULT 'singleton',
    "arkApiKey" TEXT,
    "arkBaseUrl" TEXT,
    "arkImageModel" TEXT,
    "arkVideoModel" TEXT,
    "indexTtsBaseUrl" TEXT DEFAULT 'http://localhost:7860',
    "outputDir" TEXT,
    "exportDir" TEXT,
    "exportBurnSubtitles" BOOLEAN NOT NULL DEFAULT true,
    "updatedAt" DATETIME NOT NULL
);
INSERT INTO "new_Setting" ("arkApiKey", "arkBaseUrl", "arkImageModel", "arkVideoModel", "id", "indexTtsBaseUrl", "updatedAt") SELECT "arkApiKey", "arkBaseUrl", "arkImageModel", "arkVideoModel", "id", "indexTtsBaseUrl", "updatedAt" FROM "Setting";
DROP TABLE "Setting";
ALTER TABLE "new_Setting" RENAME TO "Setting";
PRAGMA foreign_keys=ON;
PRAGMA defer_foreign_keys=OFF;
