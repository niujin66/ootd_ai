package com.ootd.ai.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * P1 回归：目录与推荐解析要能拒绝坏输入。
 *
 * 旧实现用 `optString` 一路往下读，缺字段给空串、类型错也不报，
 * 坏包能一路走到下载甚至落成生效版本。这里的每一条都对应一种坏输入。
 */
class CatalogParserTest {

    private val goodSha = "a".repeat(64)

    private fun catalogJson(
        personId: String = "\"person-001\"",
        version: String = "\"fixture-face-03\"",
        assets: String = goodAssets()
    ) = """
        {
          "person_id": $personId,
          "catalog_version": $version,
          "pose_id": "pose-001",
          "assets": $assets
        }
    """.trimIndent()

    private fun goodAssets() = """
        [
          {
            "asset_id": "person-001", "asset_type": "person", "display_name": "人物",
            "tags": ["base"], "available": true,
            "asset_url": "/v1/assets/person-001/fixture-face-03/person-001",
            "size_bytes": 419572, "sha256": "$goodSha"
          },
          {
            "asset_id": "top-001_v2", "asset_type": "garment", "display_name": "黑上衣",
            "tags": ["dark"], "available": true,
            "asset_url": "/v1/assets/person-001/fixture-face-03/top-001_v2",
            "size_bytes": 981408, "sha256": "${"b".repeat(64)}"
          }
        ]
    """.trimIndent()

    // ---------------- 目录 ----------------

    @Test
    fun `正常目录能解析出人物和衣物`() {
        val catalog = CatalogParser.parseCatalog(catalogJson())

        assertEquals("person-001", catalog.personId)
        assertEquals(2, catalog.assets.size)
        assertEquals("person-001", catalog.person?.assetId)
        assertEquals(listOf("top-001_v2"), catalog.garments.map { it.assetId })
    }

    @Test
    fun `缺 person_id 直接拒绝`() {
        val json = """{"catalog_version":"v1","assets":${goodAssets()}}"""
        assertRejected { CatalogParser.parseCatalog(json) }
    }

    @Test
    fun `缺 assets 数组直接拒绝`() {
        val json = """{"person_id":"p","catalog_version":"v1"}"""
        assertRejected { CatalogParser.parseCatalog(json) }
    }

    @Test
    fun `资产缺 sha256 直接拒绝`() {
        val assets = goodAssets().replace("\"sha256\": \"$goodSha\"", "\"sha256\": null")
        assertRejected { CatalogParser.parseCatalog(catalogJson(assets = assets)) }
    }

    @Test
    fun `sha256 不是 64 位十六进制直接拒绝`() {
        val assets = goodAssets().replace(goodSha, "abc123")
        assertRejected { CatalogParser.parseCatalog(catalogJson(assets = assets)) }
    }

    @Test
    fun `size_bytes 是字符串直接拒绝`() {
        val assets = goodAssets().replace("\"size_bytes\": 419572", "\"size_bytes\": \"419572\"")
        assertRejected { CatalogParser.parseCatalog(catalogJson(assets = assets)) }
    }

    @Test
    fun `size_bytes 为 0 直接拒绝`() {
        val assets = goodAssets().replace("\"size_bytes\": 419572", "\"size_bytes\": 0")
        assertRejected { CatalogParser.parseCatalog(catalogJson(assets = assets)) }
    }

    @Test
    fun `asset_id 重复直接拒绝`() {
        val assets = """
            [
              {"asset_id":"person-001","asset_type":"person","display_name":"p","tags":[],
               "available":true,"asset_url":"/a","size_bytes":10,"sha256":"$goodSha"},
              {"asset_id":"person-001","asset_type":"person","display_name":"p2","tags":[],
               "available":true,"asset_url":"/b","size_bytes":10,"sha256":"${"c".repeat(64)}"}
            ]
        """.trimIndent()
        assertRejected { CatalogParser.parseCatalog(catalogJson(assets = assets)) }
    }

    @Test
    fun `asset_id 含非法字符直接拒绝`() {
        val assets = goodAssets().replace("\"person-001\"", "\"person 001\"")
        assertRejected { CatalogParser.parseCatalog(catalogJson(assets = assets)) }
    }

    @Test
    fun `未知 asset_type 直接拒绝`() {
        val assets = goodAssets().replace("\"person\"", "\"shoes\"")
        assertRejected { CatalogParser.parseCatalog(catalogJson(assets = assets)) }
    }

    @Test
    fun `没有人物条目直接拒绝`() {
        val assets = """
            [
              {"asset_id":"top-1","asset_type":"garment","display_name":"g","tags":[],
               "available":true,"asset_url":"/a","size_bytes":10,"sha256":"$goodSha"}
            ]
        """.trimIndent()
        assertRejected { CatalogParser.parseCatalog(catalogJson(assets = assets)) }
    }

    // ---------------- 推荐 ----------------

    private fun recommendationJson(
        status: String = "\"ok\"",
        mode: String = "\"mock\"",
        garmentId: String = "\"top-001_v2\""
    ) = """
        {
          "request_id": "req-abc12345",
          "catalog_version": "fixture-face-03",
          "status": $status,
          "garment_id": $garmentId,
          "reason": "符合场合",
          "mode": $mode
        }
    """.trimIndent()

    @Test
    fun `正常推荐能解析`() {
        val r = CatalogParser.parseRecommendation(recommendationJson())

        assertEquals("ok", r.status)
        assertEquals("mock", r.mode)
        assertEquals("top-001_v2", r.garmentId)
        assertTrue(r.isMock)
    }

    @Test
    fun `未知 status 直接拒绝`() {
        assertRejected {
            CatalogParser.parseRecommendation(recommendationJson(status = "\"maybe\""))
        }
    }

    @Test
    fun `未知 mode 直接拒绝`() {
        assertRejected {
            CatalogParser.parseRecommendation(recommendationJson(mode = "\"gpt\""))
        }
    }

    @Test
    fun `status 为 ok 但没给 garment_id 直接拒绝`() {
        assertRejected {
            CatalogParser.parseRecommendation(recommendationJson(garmentId = "null"))
        }
    }

    @Test
    fun `no_match 允许没有 garment_id`() {
        val r = CatalogParser.parseRecommendation(
            recommendationJson(status = "\"no_match\"", garmentId = "null")
        )

        assertEquals("no_match", r.status)
        assertTrue(r.isNoMatch)
    }

    // ---------------- 衣物是否在本次目录里 ----------------

    private fun result(garmentId: String?) = RecommendationResult(
        requestId = "req-1",
        catalogVersion = "v1",
        status = if (garmentId == null) "no_match" else "ok",
        garmentId = garmentId,
        reason = "",
        mode = "mock"
    )

    @Test
    fun `推荐了目录里没有的衣物会被拦下`() {
        val reason = RecommendationGuard.rejectReason(result("top-999"), setOf("top-001_v2"))

        assertTrue("应当给出拒绝理由", reason != null)
        assertTrue(reason!!.contains("top-999"))
    }

    @Test
    fun `推荐了目录里有的衣物放行`() {
        assertNull(RecommendationGuard.rejectReason(result("top-001_v2"), setOf("top-001_v2")))
    }

    @Test
    fun `没有匹配不算错误`() {
        assertNull(RecommendationGuard.rejectReason(result(null), emptySet()))
    }

    private fun assertRejected(block: () -> Unit) {
        var threw = false
        try {
            block()
        } catch (e: IllegalArgumentException) {
            threw = true
        } catch (e: org.json.JSONException) {
            threw = true
        }
        assertTrue("坏输入应当被拒绝（抛异常），但这次通过了", threw)
    }
}
