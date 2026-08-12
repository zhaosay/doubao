-- CreateTable
CREATE TABLE "Setting" (
    "id" TEXT NOT NULL PRIMARY KEY DEFAULT 'singleton',
    "arkApiKey" TEXT,
    "indexTtsBaseUrl" TEXT DEFAULT 'http://localhost:7860',
    "updatedAt" DATETIME NOT NULL
);
