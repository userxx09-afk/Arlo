import java.util.Properties

plugins {
    id("com.android.application")
}

// Signing: signing.properties (next to settings.gradle.kts, not checked in) says where BAN's
// own key is. Android only installs an update over the old app if both use the same key,
// so always build releases with it. Without the file the build uses this machine's debug key.
val signing = Properties().apply {
    rootProject.file("signing.properties").takeIf { it.exists() }?.inputStream()?.use { load(it) }
}

android {
    namespace = "local.ban"
    compileSdk = 36
    buildToolsVersion = "36.0.0"

    defaultConfig {
        applicationId = "local.ban"
        minSdk = 26
        targetSdk = 35
        versionCode = (findProperty("banVersionCode") as String?)?.toInt() ?: 1
        versionName = (findProperty("banVersionName") as String?) ?: "1.0"
    }

    signingConfigs {
        if (signing.getProperty("storeFile") != null) create("ban") {
            storeFile = file(signing.getProperty("storeFile"))
            storePassword = signing.getProperty("storePassword")
            keyAlias = signing.getProperty("keyAlias") ?: "ban"
            keyPassword = signing.getProperty("keyPassword") ?: signing.getProperty("storePassword")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.findByName("ban") ?: signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    lint {
        checkReleaseBuilds = false
        abortOnError = false
    }
}
