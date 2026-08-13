from fastapi import APIRouter

from app.providers.seedream import IMAGE_RATIOS

# 给"新建短剧"(Project.aspectRatio)和"图生视频"(VideoGeneration.ratio)两个表单的比例
# 选择器用。海报(/posters/options)、独立文生图(/text-images/options)已经各自有一份
# 专属的 /options 端点(历史原因，字段名叫 orientations)，这里不重复造，只给新加的
# 两个消费方一个统一入口——四处消费的其实是同一份 IMAGE_RATIOS 词典。
router = APIRouter(tags=["media-ratios"])


@router.get("/media-ratios")
def list_media_ratios():
    return {"ratios": [{"id": rid, "label": cfg["label"]} for rid, cfg in IMAGE_RATIOS.items()]}
