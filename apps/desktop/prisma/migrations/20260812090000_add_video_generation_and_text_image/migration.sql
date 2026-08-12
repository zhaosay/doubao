-- CreateTable
CREATE TABLE "VideoGeneration" (
    "id" TEXT NOT NULL PRIMARY KEY,
    "projectId" TEXT,
    "referenceImagePath" TEXT NOT NULL,
    "prompt" TEXT NOT NULL,
    "filePath" TEXT,
    "status" TEXT NOT NULL DEFAULT 'pending',
    "error" TEXT,
    "providerId" TEXT,
    "model" TEXT,
    "createdAt" DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "VideoGeneration_projectId_fkey" FOREIGN KEY ("projectId") REFERENCES "Project" ("id") ON DELETE SET NULL ON UPDATE CASCADE
);

-- CreateIndex
CREATE INDEX "VideoGeneration_projectId_idx" ON "VideoGeneration"("projectId");

-- CreateTable
CREATE TABLE "TextImage" (
    "id" TEXT NOT NULL PRIMARY KEY,
    "projectId" TEXT,
    "prompt" TEXT NOT NULL,
    "orientation" TEXT NOT NULL DEFAULT 'portrait',
    "styleMode" TEXT NOT NULL DEFAULT 'comic',
    "referenceImagePaths" TEXT,
    "filePath" TEXT,
    "status" TEXT NOT NULL DEFAULT 'pending',
    "error" TEXT,
    "providerId" TEXT,
    "model" TEXT,
    "createdAt" DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "TextImage_projectId_fkey" FOREIGN KEY ("projectId") REFERENCES "Project" ("id") ON DELETE SET NULL ON UPDATE CASCADE
);

-- CreateIndex
CREATE INDEX "TextImage_projectId_idx" ON "TextImage"("projectId");
