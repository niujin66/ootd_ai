package com.ootd.ai

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.tooling.preview.Preview
import androidx.compose.ui.unit.dp
import com.ootd.ai.ui.TryOnScreen
import com.ootd.ai.ui.theme.AndroidTheme

/**
 * 「衣序」入口。
 *
 * 首页显示应用名，点「进入试衣」进三维页（B02）。
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

/** 首页：应用名 + 进入试衣入口。 */
@Composable
fun HomeScreen(
    onEnterTryOn: () -> Unit = {},
    modifier: Modifier = Modifier
) {
    Column(
        modifier = modifier.fillMaxSize(),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        Text(
            text = "衣序",
            style = MaterialTheme.typography.headlineLarge
        )
        Spacer(Modifier.height(12.dp))
        Text(
            text = "本人三维人物 · 换装查看",
            style = MaterialTheme.typography.bodyMedium,
            textAlign = TextAlign.Center
        )
        Spacer(Modifier.height(28.dp))
        Button(onClick = onEnterTryOn) {
            Text("进入试衣")
        }
    }
}

@Preview(showBackground = true)
@Composable
fun HomeScreenPreview() {
    AndroidTheme {
        HomeScreen()
    }
}