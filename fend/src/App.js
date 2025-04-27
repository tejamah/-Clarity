import React, { useEffect, useState } from "react";
import io from "socket.io-client";

const socket = io("http://127.0.0.1:5000");

const categories = ["technology", "business", "sports", "entertainment"];

function App() {
  const [category, setCategory] = useState("technology");
  const [articles, setArticles] = useState([]);

  useEffect(() => {
    fetch(`http://127.0.0.1:5000/news/${category}`)
      .then((response) => response.json())
      .then((data) => setArticles(data));

    socket.on("news_update", (updatedNews) => {
      if (updatedNews[category]) {
        setArticles(updatedNews[category]);
      }
    });

    return () => socket.off("news_update");
  }, [category]);

  return (
    <div style={{ padding: 20, fontFamily: "Arial" }}>
      <h1>📰 AI News Summarizer</h1>
      <div>
        {categories.map((cat) => (
          <button
            key={cat}
            onClick={() => setCategory(cat)}
            style={{
              margin: "5px",
              padding: "10px 15px",
              backgroundColor: cat === category ? "#1e90ff" : "#f0f0f0",
              color: cat === category ? "white" : "black",
              border: "none",
              borderRadius: "5px",
              cursor: "pointer"
            }}
          >
            {cat.charAt(0).toUpperCase() + cat.slice(1)}
          </button>
        ))}
      </div>
      <div>
        {articles.map((article, index) => (
          <div key={index} style={{ border: "1px solid #ddd", padding: 15, margin: 10, borderRadius: 10 }}>
            <h3>{article.title}</h3>
            {article.image && <img src={article.image} alt="" style={{ maxWidth: "100%", height: "auto" }} />}
            <p>{article.summary}</p>
            <a href={article.url} target="_blank" rel="noopener noreferrer">🔗 Read Full Article</a>
          </div>
        ))}
      </div>
    </div>
  );
}

export default App;
