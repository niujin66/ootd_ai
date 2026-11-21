package com.ootd.ai

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.tooling.preview.Preview
import androidx.compose.ui.unit.dp
import com.ootd.ai.ui.TryOnScreen
import com.ootd.ai.ui.theme.AndroidTheme

/**
 * 「衣序」入口。
 *
 * 首页负责「第一次打开就知道这个应用怎么用」：#27 要求给出三步引导，
 * 让用户从「选场合」开始，而不是进来只看到一个「进入试衣」按钮不知道干什么。
 * 三维场景与衣物切换在 TryOnScreen 里，这里不持有渲染逻辑。
 */
class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        setContent {
            AndroidTheme {
                var showTryOn by remember { mutableStateOf(false) }

                if (showTryOn) {
                    TryOnScreen(onBack = { showTryOn = false })
                } else {
                    Scaffold(modifier = Modifier.fillMaxSize()) { innerPadding ->
                        HomeScreen(
                            onEnterTryOn = { showTryOn = true },
                            modifier = Modifier.padding(innerPadding)
                        )
                    }
                }
            }
        }
    }
}

/**
 * 首页：应用名 + 三步引导 + 进入试衣入口。
 *
 * 两条硬要求（#27）：
 * 1. 小屏 / 大字号下**入口按钮必须还在**。所以按钮固定在底部，不跟着内容滚走；
 * 2. 内容本身可以滚动，字放大到 2 倍也不会被裁掉。
 */
@Composable
fun HomeScreen(
    onEnterTryOn: () -> Unit = {},
    modifier: Modifier = Modifier
) {
    Box(modifier = modifier.fillMaxSize()) {
        // 引导内容：超出屏幕时可滚动；底部留出按钮的高度，避免最后一步被按钮压住
        Column(
            modifier = Modifier
                .fillMaxSize()
                .verticalScroll(rememberScrollState())
                .padding(start = 24.dp, end = 24.dp, top = 40.dp, bottom = 112.dp),
            horizontalAlignment = Alignment.CenterHorizontally
        ) {
            Text(
                text = "衣序",
                style = MaterialTheme.typography.headlineLarge,
                textAlign = TextAlign.Center
            )
            Spacer(Modifier.height(8.dp))
            Text(
                text = "本人三维人物 · 换装查看",
                style = MaterialTheme.typography.bodyMedium,
                textAlign = TextAlign.Center
            )

            Spacer(Modifier.height(32.dp))

            GuideStep(
                index = 1,
                title = "选择场合与偏好",
                detail = "先说要穿去哪儿，比如约会、上班；再挑喜欢的色调，浅色或深色都行"
            )
            Spacer(Modifier.height(20.dp))
            GuideStep(
                index = 2,
                title = "查看推荐并换衣",
                detail = "看它推荐哪一件，点一下就换上；也可以自己从列表里挑"
            )
            Spacer(Modifier.height(20.dp))
            GuideStep(
                index = 3,
                title = "拖动查看正侧背",
                detail = "单指拖动转视角，正面侧面背面都看一眼，确认颜色和版型"
            )
        }

        // 入口固定在底部：无论内容多长、字多大，它都在
        Surface(
            modifier = Modifier.align(Alignment.BottomCenter).fillMaxWidth(),
            tonalElevation = 3.dp
        ) {
            Button(
                onClick = onEnterTryOn,
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(horizontal = 24.dp, vertical = 16.dp)
                    .heightIn(min = 48.dp)
            ) {
                Text("进入试衣")
            }
        }
    }
}

/** 一条引导：左边序号，右边标题和一句说明。 */
@Composable
private fun GuideStep(
    index: Int,
    title: String,
    detail: String
) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.Top
    ) {
        Box(
            modifier = Modifier
                .size(28.dp)
                .clip(CircleShape)
                .background(MaterialTheme.colorScheme.primaryContainer),
            contentAlignment = Alignment.Center
        ) {
            Text(
                text = index.toString(),
                style = MaterialTheme.typography.labelLarge,
                color = MaterialTheme.colorScheme.onPrimaryContainer
            )
        }
        Spacer(Modifier.width(12.dp))
        Column(modifier = Modifier.weight(1f)) {
            Text(
                text = title,
                style = MaterialTheme.typography.titleMedium
            )
            Spacer(Modifier.height(4.dp))
            Text(
                text = detail,
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }
    }
}

@Preview(showBackground = true, name = "正常字号")
@Composable
fun HomeScreenPreview() {
    AndroidTheme {
        HomeScreen()
    }
}

/** 大字号预览：验证字号放大到 2 倍时按钮仍在、内容可滚。 */
@Preview(showBackground = true, name = "大字号", fontScale = 2f, heightDp = 480)
@Composable
fun HomeScreenLargeFontPreview() {
    AndroidTheme {
        HomeScreen()
    }
}
